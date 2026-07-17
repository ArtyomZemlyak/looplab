"""ConceptFrame derives co_occurs from the current bounded membership snapshot for every run.

Legacy persisted counts are ignored because their max-fold could never retract decreases/removals.
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


def test_persisted_cooccurs_is_replaced_by_current_count(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/dcl", "hyperparameter/temperature"]))
    s.append("node_created", _created(1, ["loss/dcl", "hyperparameter/temperature"]))
    # The legacy cache says 5, but the current folded membership says exactly 2.
    s.append("concept_edge", {"src": "loss/dcl", "rel": "co_occurs", "dst": "hyperparameter/temperature",
                              "provenance": "evidenced", "confidence": 5})
    inp = bounded_inputs(fold(s.read_all()), default_lenses())
    co = [v for k, v in inp["edges"].items() if k[1] == "co_occurs"]
    assert len(co) == 1 and co[0]["provenance"] == "co-tag (derived)"
    assert co[0]["confidence"] == 2.0


def test_persisted_ghost_pair_disappears_after_retag(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/dcl", "hyperparameter/temperature"]))
    s.append("node_created", _created(1, ["loss/dcl"]))
    s.append("concept_edge", {
        "src": "loss/dcl", "rel": "co_occurs", "dst": "hyperparameter/temperature",
        "provenance": "evidenced", "confidence": 99,
    })

    inp = bounded_inputs(fold(s.read_all()), default_lenses())

    assert not any(key[1] == "co_occurs" for key in inp["edges"])
    assert inp["source_edges"] == 0


def test_explicit_non_derived_edge_survives_alongside_derived_cooccurs(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/dcl", "hyperparameter/temperature"]))
    s.append("node_created", _created(1, ["loss/dcl", "hyperparameter/temperature"]))
    s.append("concept_edge", {
        "src": "loss/dcl", "rel": "uses", "dst": "optimizer/adam",
        "provenance": "asserted", "confidence": 0.8,
    })

    inp = bounded_inputs(fold(s.read_all()), default_lenses())

    assert ("loss/dcl", "uses", "optimizer/adam") in inp["edges"]
    assert any(key[1] == "co_occurs" for key in inp["edges"])


def test_large_legacy_cooccurrence_cache_cannot_starve_explicit_edges(tmp_path):
    from looplab.serve.concept_frame import MAX_EDGES

    s = _store(tmp_path)
    legacy = [
        {"src": f"legacy/{index}", "rel": "co_occurs", "dst": f"other/{index}",
         "provenance": "evidenced", "confidence": 2}
        for index in range(MAX_EDGES + 10)
    ]
    legacy.append({
        "src": "model/rag", "rel": "uses", "dst": "index/hnsw",
        "provenance": "asserted", "confidence": 1.0,
    })
    s.append("concept_edge", {"edges": legacy})

    inp = bounded_inputs(fold(s.read_all()), default_lenses())

    assert inp["source_edges"] == 1
    assert ("model/rag", "uses", "index/hnsw") in inp["edges"]
    assert "edge_cap" not in inp["reasons"]
