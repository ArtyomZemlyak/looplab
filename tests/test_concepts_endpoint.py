"""Phase 3c: the GET /api/runs/{id}/concepts serve endpoint — per-lens hierarchy + per-concept
metrics/Δ + the lens pack, end to end (fold -> read-models -> JSON). Pure; recomputed each call."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient                       # noqa: E402

from looplab.events.eventstore import EventStore                # noqa: E402
from looplab.serve.server import make_app                       # noqa: E402


def _demo_run(root):
    rd = root / "demo"
    rd.mkdir(parents=True, exist_ok=True)
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "r",
                                       "concepts": ["loss/contrastive/dcl", "architecture/moe"]}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.9})
    s.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                              "idea": {"operator": "improve", "params": {}, "rationale": "r",
                                       "concepts": ["loss/contrastive/mnr"]}})
    s.append("node_evaluated", {"node_id": 1, "metric": 0.7})
    return rd


def test_concepts_endpoint_is_a_lens(tmp_path):
    _demo_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    r = client.get("/api/runs/demo/concepts")
    assert r.status_code == 200
    data = r.json()
    assert data["lens"] == "is_a"
    assert [l["name"] for l in data["lenses"]][0] == "is_a"       # the lens pack ships, is_a default
    nodes = data["tree"]["nodes"]
    # the is_a tree materializes the full path chain from the authored deep tags
    assert {"loss", "loss/contrastive", "loss/contrastive/dcl", "loss/contrastive/mnr",
            "architecture", "architecture/moe"} <= set(nodes)
    assert nodes["loss/contrastive/dcl"]["tagged"] is True
    assert nodes["loss"]["tagged"] is False                       # synthetic ancestor group
    # per-concept metrics reach the UI (multi-membership node 0 counts fully in both its concepts)
    rows = data["metrics"]["rows"]
    assert rows["loss/contrastive/dcl"]["best"] == 0.9
    assert rows["architecture/moe"]["best"] == 0.9
    assert data["touch"]["loss/contrastive/dcl"] == 1


def test_concepts_endpoint_unknown_run_is_handled(tmp_path):
    client = TestClient(make_app(tmp_path))
    r = client.get("/api/runs/nope/concepts")
    assert r.status_code in (200, 404)                           # resolved-empty or not-found, never 500
