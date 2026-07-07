"""Live trace: the node inspector must be able to fetch a node's trace WHILE it's still building — its
create_node span hasn't closed yet so the node isn't in state, but its sub-spans flush live. Before the
fix node_detail 404'd until the build finished, so the Trace tab showed only a placeholder then filled in
all at once at the end. Now a building node serves an in-progress detail carrying the live trace."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


def _seed(rd):
    rd.mkdir(parents=True, exist_ok=True)
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": rd.name, "task_id": "t", "goal": "g", "direction": "min"})
    return s


def test_node_detail_serves_a_building_node(tmp_path):
    rd = tmp_path / "r1"
    s = _seed(rd)
    # a node whose build STARTED (node_building) but has no node_created yet — the "writing code…" window
    s.append("node_building", {"node_id": 5, "operator": "draft", "parent_ids": []})
    client = TestClient(make_app(tmp_path))

    r = client.get("/api/runs/r1/nodes/5")
    assert r.status_code == 200, "a building node must serve its in-progress detail, not 404"
    body = r.json()
    assert body["status"] == "building" and body["id"] == 5
    assert "trace" in body                      # the (possibly still-empty) live trace container

    # a node that is neither built nor building still 404s (no silent 200 for a bogus id)
    assert client.get("/api/runs/r1/nodes/9").status_code == 404


def test_built_node_still_served_normally(tmp_path):
    rd = tmp_path / "r2"
    s = _seed(rd)
    s.append("node_building", {"node_id": 0, "operator": "draft", "parent_ids": []})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": "print(1)"})
    client = TestClient(make_app(tmp_path))
    r = client.get("/api/runs/r2/nodes/0")
    assert r.status_code == 200 and r.json().get("status") != "building"  # a real node, full detail
