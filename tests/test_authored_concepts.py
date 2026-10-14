"""UI review 2026-10-14 sections 4.3 / 4.4 (doc 80 §9 item 7): an external run's concepts get a
hierarchy and a cross-run record, both labelled as AUTHORED and kept apart from measured evidence.

* `concept_edge` is now a control intent: an agent (or the operator) declares `is_a` / `part_of`
  edges; the intake validates ids, refuses a cycle and stamps `provenance: authored`. The fold
  already reads `concept_edge`, so no recorded run changes; the is_a hierarchy nests a flat id
  under its declared parent (`search/concept_lens.py::apply_authored_parents`) only when such an
  edge exists.
* finalize writes an authored concept capsule (`authored_concept_capsules.jsonl`) — vocabulary and
  coverage only — beside, never inside, the classifier capsule store.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from looplab.engine.concept_capsules import (  # noqa: E402
    AuthoredConceptCapsuleStore, authored_concept_overview, valid_authored_capsule)
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.search.concept_lens import apply_authored_parents, project_hierarchy  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from tests.factories import make_engine  # noqa: E402

AGENT = {"X-LoopLab-Token": "agent-secret"}
OWNER = {"X-LoopLab-Token": "owner-secret"}


def _run(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    (rd / "config.snapshot.json").write_text(json.dumps({"backend": "toy", "external_harness": True}))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "uid-demo", "task_id": "task",
                                 "goal": "g", "direction": "max"})
    for node_id, concepts in ((0, ["data-composition", "replay"]), (1, ["replay", "rare-items"])):
        store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "concepts": concepts}})
        store.append("node_evaluated", {"node_id": node_id, "metric": 0.5 + node_id})
    return rd


def _edges(client, edges, key):
    generation = client.get("/api/runs/demo/state", headers=AGENT).json()["generation"]
    return client.post("/api/runs/demo/commands", headers={**AGENT, "Idempotency-Key": key},
                       json={"type": "concept_edge", "data": {"edges": edges},
                             "expected_generation": generation})


def test_declared_edges_fold_as_authored_and_nest_flat_ids(tmp_path, monkeypatch):
    rd = _run(tmp_path, monkeypatch)
    client = TestClient(make_app(tmp_path))
    flat = client.get("/api/runs/demo/concepts", headers=OWNER).json()
    response = _edges(client, [{"src": "replay", "rel": "is_a", "dst": "data-composition"},
                               {"src": "rare-items", "dst": "data-composition"}], "edges-1")
    assert response.status_code == 200 and response.json()["status"] != "rejected", response.text
    events = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "concept_edge"]
    assert events[-1].data["mode"] == "authored" and events[-1].data["actor_kind"] == "harness_agent"
    state = fold(EventStore(rd / "events.jsonl").read_all())
    assert {(e["src"], e["dst"], e["provenance"]) for e in state.concept_edges.values()} == {
        ("replay", "data-composition", "authored"), ("rare-items", "data-composition", "authored")}
    nested = client.get("/api/runs/demo/concepts", headers=OWNER).json()
    assert flat != nested
    # A cycle is refused at intake, and so is an unknown relation.
    for edges, key in (([{"src": "data-composition", "dst": "replay"}], "cycle"),
                       ([{"src": "a", "rel": "uses", "dst": "b"}], "rel")):
        refused = _edges(client, edges, key)
        assert refused.status_code == 400 or refused.json()["status"] == "rejected", refused.text


def test_the_is_a_projection_moves_only_roots_and_only_for_authored_edges():
    tree = project_hierarchy(["data-composition", "replay", "loss/contrastive"])
    asserted = {"k": {"src": "replay", "rel": "is_a", "dst": "data-composition",
                      "provenance": "asserted", "confidence": 1.0}}
    before = json.dumps(tree, sort_keys=True)
    assert json.dumps(apply_authored_parents(tree, asserted), sort_keys=True) == before, (
        "an engine-asserted edge (every recorded run) leaves the path tree byte-identical")
    authored = {
        "a": {"src": "replay", "rel": "is_a", "dst": "data-composition", "provenance": "authored"},
        # a path child keeps its path parent; a declared parent nothing tagged is materialized.
        "b": {"src": "loss/contrastive", "rel": "is_a", "dst": "objective", "provenance": "authored"},
        "c": {"src": "loss", "rel": "is_a", "dst": "objective", "provenance": "authored"},
    }
    out = apply_authored_parents(project_hierarchy(
        ["data-composition", "replay", "loss/contrastive"]), authored)
    assert out["nodes"]["replay"]["parent"] == "data-composition"
    assert out["nodes"]["replay"]["depth"] == 1
    assert out["nodes"]["loss/contrastive"]["parent"] == "loss"
    assert out["nodes"]["loss"]["parent"] == "objective" and out["nodes"]["objective"]["tagged"] is False
    assert out["nodes"]["loss/contrastive"]["depth"] == 2
    assert out["roots"] == ["data-composition", "objective"]


def test_finalize_writes_an_authored_capsule_and_leaves_the_classifier_store_alone(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=1, memory_dir=str(memory))
    store = EventStore(tmp_path / "run" / "events.jsonl")
    store.append("run_started", {"run_id": "ext", "run_uid": "uid-ext", "task_id": "task",
                                 "goal": "g", "direction": "max"})
    for node_id, concepts in ((0, ["Replay", "data-composition"]), (1, ["replay"])):
        store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "concepts": concepts}})
        store.append("node_evaluated", {"node_id": node_id, "metric": 0.5})
    final = fold(store.read_all())
    eng.lessons.store_concept_capsule(final)
    eng.lessons.store_authored_concept_capsule(final)
    assert not (memory / "concept_capsules.jsonl").exists() or not [
        line for line in (memory / "concept_capsules.jsonl").read_text().splitlines() if line.strip()], (
        "authored labels still never become classifier evidence")
    rows = AuthoredConceptCapsuleStore(memory / "authored_concept_capsules.jsonl").all()
    assert len(rows) == 1
    row = rows[0]
    assert row["evidence_tier"] == "authored" and row["run_uid"] == "uid-ext"
    assert row["concept_nodes"] == {"data-composition": 1, "replay": 2}
    assert row["nodes_total"] == 2 and row["producers"] == ["researcher-authored"]
    assert not {"concept_outcomes", "concept_signs", "best_metric"} & set(row)
    # Idempotent upsert: a finalize retry replaces, never duplicates.
    eng.lessons.store_authored_concept_capsule(final)
    assert len(AuthoredConceptCapsuleStore(memory / "authored_concept_capsules.jsonl").all()) == 1


def test_a_row_carrying_a_measured_field_is_not_an_authored_capsule():
    good = {"v": 1, "evidence_tier": "authored", "run_id": "r", "concepts": ["x"],
            "concept_nodes": {"x": 1}, "fingerprint": []}
    assert valid_authored_capsule(good)
    for key, value in (("concept_outcomes", {"x": 1.0}), ("best_metric", 0.3),
                       ("concept_evidence", "classifier")):
        assert not valid_authored_capsule({**good, key: value}), key
    assert not valid_authored_capsule({**good, "evidence_tier": "classifier"})
    overview = authored_concept_overview([good, {**good, "run_id": "r2"}])
    assert overview["evidence_tier"] == "authored"
    assert overview["concepts"] == [{"concept": "x", "nodes": 2, "runs": [
        {"run_id": "r", "task_id": "", "nodes": 1}, {"run_id": "r2", "task_id": "", "nodes": 1}]}]


def test_the_authored_tier_is_served_labelled(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    memory = Path(os.environ["LOOPLAB_MEMORY_DIR"])
    memory.mkdir(parents=True, exist_ok=True)
    AuthoredConceptCapsuleStore(memory / "authored_concept_capsules.jsonl").add(
        {"v": 1, "evidence_tier": "authored", "run_id": "ext", "run_uid": "u", "task_id": "t",
         "concepts": ["replay"], "concept_nodes": {"replay": 3}, "fingerprint": []})
    body = TestClient(make_app(tmp_path)).get("/api/cross-run/authored-concepts").json()
    assert body["evidence_tier"] == "authored" and body["runs"] == 1
    assert body["concepts"][0]["concept"] == "replay" and body["concepts"][0]["nodes"] == 3
