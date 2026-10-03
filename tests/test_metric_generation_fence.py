"""A metric sidecar must belong to the run generation as well as the node attempt."""
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore
from looplab.serve.server import make_app


def _seed(root):
    rd = root / "demo"
    (rd / "nodes" / "node_0").mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    return rd, TestClient(make_app(root))


def _replace(rd):
    path = rd / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["ts"] += 1
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_metrics_echo_generation_and_reject_a_replaced_run_before_read(tmp_path, monkeypatch):
    from looplab.serve import metrics_adapters

    rd, client = _seed(tmp_path)
    calls = []
    monkeypatch.setattr(metrics_adapters, "fenced_node_metrics", lambda *args: calls.append(args) or {})
    generation = client.get("/api/runs/demo/state").json()["generation"]
    params = {"attempt": 0, "expected_generation": generation}
    current = client.get("/api/runs/demo/nodes/0/metrics", params=params)
    assert current.status_code == 200, current.text
    assert current.json()["run_generation"] == generation
    assert current.json()["node_id"] == 0 and current.json()["attempt"] == 0
    calls.clear()
    _replace(rd)
    stale = client.get("/api/runs/demo/nodes/0/metrics", params=params)
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "run_generation_changed"
    assert calls == [], "a mismatching generation must refuse before opening sidecars"

    # Unfenced legacy reads remain available, but still stamp the actual generation.
    latest = client.get("/api/runs/demo/nodes/0/metrics")
    assert latest.status_code == 200, latest.text
    assert latest.json()["run_generation"] != generation


@pytest.mark.parametrize("fenced", [True, False])
def test_metrics_refuse_replacement_during_adapter_read_even_without_client_fence(tmp_path, monkeypatch, fenced):
    from looplab.serve import metrics_adapters

    rd, client = _seed(tmp_path)
    generation = client.get("/api/runs/demo/state").json()["generation"]

    def read(*args):
        _replace(rd)
        return {"old/loss": [{"step": 1, "value": 99}]}

    monkeypatch.setattr(metrics_adapters, "fenced_node_metrics", read)
    response = client.get("/api/runs/demo/nodes/0/metrics",
                          params={"expected_generation": generation} if fenced else {})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "run_generation_changed"
    assert "old/loss" not in response.text


@pytest.mark.parametrize("generation", ["", "wrong", "A" * 64])
def test_metrics_refuse_invalid_generation(tmp_path, generation):
    _, client = _seed(tmp_path)
    response = client.get("/api/runs/demo/nodes/0/metrics", params={"expected_generation": generation})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "invalid_run_generation"


def test_metrics_still_refuse_an_attempt_reset_during_read(tmp_path, monkeypatch):
    from looplab.serve import metrics_adapters

    rd, client = _seed(tmp_path)
    generation = client.get("/api/runs/demo/state").json()["generation"]

    def read(*args):
        EventStore(rd / "events.jsonl").append("node_reset", {
            "node_id": 0, "generation": 0, "from_stage": "eval"})
        return {"old/loss": [{"step": 1, "value": 99}]}

    monkeypatch.setattr(metrics_adapters, "fenced_node_metrics", read)
    response = client.get("/api/runs/demo/nodes/0/metrics",
                          params={"attempt": 0, "expected_generation": generation})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "node_attempt_changed"
    assert "old/loss" not in response.text
