"""PART V Phase 2b: the durable /commands endpoint appends an operator concept re-tag (generation-fenced,
canonicalized). Mirrors the comment lifecycle: command-only (the legacy /control route rejects it), the
server validates node existence + node_generation + each concept id, and the fold stamps OPERATOR
provenance."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from looplab.core.models import NODE_CONCEPT_PROVENANCE_OPERATOR
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.serve.server import make_app


def _seed(root, run_id="demo"):
    rd = root / run_id
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "task_id": "task", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "base"}})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.9})
    return rd


def _generation(client):
    return client.get("/api/runs/demo/state").json()["generation"]


def _command(client, data, key, *, generation=None):
    return client.post("/api/runs/demo/commands", headers={"Idempotency-Key": key},
                       json={"type": "concept_tag_edited", "data": data,
                             "expected_generation": generation or _generation(client)})


def test_operator_retag_command_appends_and_folds_operator_provenance(tmp_path):
    rd = _seed(tmp_path)
    client = TestClient(make_app(tmp_path))
    # mixed-case ids are canonicalized to the /concepts vocabulary; duplicates are deduped.
    resp = _command(client, {"node_id": 0, "node_generation": 0,
                             "concepts": ["Loss/Contrastive", "arch/moe", "loss/contrastive"]},
                    "retag-1")
    assert resp.status_code == 200 and resp.json()["status"] == "succeeded"
    rows = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "concept_tag_edited"]
    assert len(rows) == 1
    assert rows[0].data["concepts"] == ["loss/contrastive", "arch/moe"]      # canonical + deduped
    st = fold(EventStore(rd / "events.jsonl").read_all())
    assert st.node_concepts[0] == ["loss/contrastive", "arch/moe"]
    assert st.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_OPERATOR


def test_stale_generation_is_rejected(tmp_path):
    # The command protocol records validation failures as 200 REJECTED records (like comments), not HTTP.
    _seed(tmp_path)
    client = TestClient(make_app(tmp_path))
    resp = _command(client, {"node_id": 0, "node_generation": 7, "concepts": ["loss/a"]}, "stale")
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    assert resp.json()["error"]["code"] == "node_generation_changed"


def test_invalid_concept_id_and_unknown_node_are_rejected(tmp_path):
    _seed(tmp_path)
    client = TestClient(make_app(tmp_path))
    bad = _command(client, {"node_id": 0, "node_generation": 0, "concepts": ["has space/x", 5]}, "bad")
    assert bad.status_code == 200 and bad.json()["status"] == "rejected"
    assert bad.json()["error"]["code"] == "invalid_command"          # 400-class string detail
    assert "invalid concept id" in bad.json()["error"]["message"]
    missing = _command(client, {"node_id": 99, "node_generation": 0, "concepts": ["loss/a"]}, "missing")
    assert missing.status_code == 200 and missing.json()["status"] == "rejected"
    assert missing.json()["error"]["code"] == "command_target_not_found"   # 404-class string detail


def test_legacy_control_route_rejects_the_command_only_event(tmp_path):
    _seed(tmp_path)
    client = TestClient(make_app(tmp_path))
    resp = client.post("/api/runs/demo/control",
                       json={"type": "concept_tag_edited",
                             "data": {"node_id": 0, "node_generation": 0, "concepts": ["loss/a"]}})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "command_protocol_required"
