"""A cross-run import carries the experiment, never the SOURCE run's graph references.

`events/node_import.py::node_import_payload` snapshots the source node's idea. Two of its fields
point INTO the source run: `parent_card_id` (a research question on the source board) and `uses`
(artifact nodes by source-run id). The inject intake refuses both inside an idea — an unknown
question as `inject_question_invalid`, an in-idea `uses`/`node_kind` with "put node_kind and uses at
the top level" — so before the fix every import of a FILED experiment, or of an artifact producer or
consumer, was refused with a remediation the operator could not apply: the server built that idea.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.events.node_import import node_import_payload
from looplab.events.replay import fold
from looplab.serve.server import make_app
from tests.factories import command_terminal, post_command


def _run(root, name, nodes):
    rd = root / name
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": name, "run_uid": f"uid-{name}", "task_id": "task",
                                 "goal": "g", "direction": "max"})
    for node_id, extra in nodes:
        store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                      "code": "print('{\"metric\": 0.5}')",
                                      "idea": {"operator": "draft", **extra}})
        store.append("node_evaluated", {"node_id": node_id, "metric": 0.5})
    return rd


def _source(tmp_path):
    # #0 an artifact producer; #1 a consumer of #0 filed under the SOURCE board's question.
    store = EventStore(_run(tmp_path, "source", [(0, {"node_kind": "artifact"})])
                       / "events.jsonl")
    store.append("hypothesis_added", {"id": "card-q", "statement": "Does replay help?",
                                      "source": "operator"})
    store.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "draft",
                                  "code": "print('{\"metric\": 0.7}')",
                                  "idea": {"operator": "draft", "parent_card_id": "card-q",
                                           "uses": [0]}})
    store.append("node_evaluated", {"node_id": 1, "metric": 0.7})


def test_the_snapshot_drops_source_graph_references_and_keeps_the_kind(tmp_path):
    _source(tmp_path)
    state = fold(EventStore(tmp_path / "source" / "events.jsonl").read_all())
    assert state.nodes[1].idea.parent_card_id == "card-q" and state.nodes[1].idea.uses == [0]
    consumer = node_import_payload(state, 1, "source")["idea"]
    assert "parent_card_id" not in consumer and "uses" not in consumer
    producer = node_import_payload(state, 0, "source")["idea"]
    assert producer.get("node_kind") == "artifact", "what the code IS crosses runs"


def test_a_filed_consumer_and_an_artifact_producer_import_into_another_run(tmp_path):
    _source(tmp_path)
    _run(tmp_path, "destination", [(0, {})])
    client = TestClient(make_app(tmp_path))
    for node, key in ((1, "import-consumer"), (0, "import-producer")):
        response = post_command(client, "inject_node",
                                {"source_run": "source", "source_node": node}, key,
                                run_id="destination")
        assert response.status_code == 200, response.text
        record = command_terminal(client, response.json(), run_id="destination")
        assert record.get("event_seq") is not None, record
    rows = [e.data for e in EventStore(tmp_path / "destination" / "events.jsonl").read_all()
            if e.type == "inject_node"]
    consumer, producer = rows
    # The intake re-serializes the idea, so the field is present and EMPTY: filed nowhere here.
    assert consumer["idea"].get("parent_card_id") is None and "uses" not in consumer["idea"]
    assert "uses" not in consumer and "node_kind" not in consumer
    assert producer["node_kind"] == "artifact" and "node_kind" not in producer["idea"]
