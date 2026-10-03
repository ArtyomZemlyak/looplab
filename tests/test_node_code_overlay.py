"""Code comparison uses saved file overlays and creation-bound parent attempts, never workdirs."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.serve.server import make_app


def _run(root):
    rd = root / "code"
    rd.mkdir()
    events = EventStore(rd / "events.jsonl")
    events.append("run_started", {"run_id": "code", "task_id": "t", "goal": "g", "direction": "min"})
    for nid, parents, files, deleted in [
        (0, [], {"recipe.env": "MOMENTUM=0.2\n", "old.py": "print('old')\n"}, ["disabled.py"]),
        (1, [0], {"recipe.env": "MOMENTUM=0.3\n", "new.py": "print('new')\n"}, ["old.py"]),
    ]:
        events.append("node_created", {"node_id": nid, "parent_ids": parents, "operator": "draft",
            "code": "", "files": files, "deleted": deleted,
            "idea": {"operator": "draft", "params": {}, "rationale": ""}})
        events.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": nid + 1})
    return rd, events


def test_owner_overlay_and_history_refuse_to_substitute_reset_parent(tmp_path):
    _, events = _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    snapshot = client.get("/api/runs/code/state").json()
    detail = client.get("/api/runs/code/nodes/1").json()
    parent = detail["parent_edit"]
    assert parent == {"version": 1, "scope": "node_edit_overlay", "node_id": 0, "attempt": 0,
        "code": "", "files": {"recipe.env": "MOMENTUM=0.2\n", "old.py": "print('old')\n"},
        "deleted": ["disabled.py"], "base_revision": None}
    assert detail["files"]["recipe.env"] == "MOMENTUM=0.3\n"
    assert client.get("/api/runs/code/nodes/0").json()["parent_edit"] is None
    events.append("node_reset", {"node_id": 0, "generation": 0})
    assert client.get("/api/runs/code/nodes/1").json()["parent_edit"] is None
    history = client.get("/api/runs/code/nodes/1", params={
        "seq": snapshot["seq"], "expected_generation": snapshot["generation"]})
    assert history.status_code == 200
    assert history.json()["parent_edit"] == parent


def test_review_overlay_is_opt_in_and_secret_scrubbed(tmp_path, monkeypatch):
    _, events = _run(tmp_path)
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    # A second child reads the same saved parent, including its secret-shaped source text.
    events.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "draft",
        "code": "", "files": {"secret.py": f"token='{secret}'\n"},
        "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    events.append("node_created", {"node_id": 3, "parent_ids": [2], "operator": "improve",
        "code": "", "files": {"recipe.env": "MOMENTUM=0.4\n"},
        "idea": {"operator": "improve", "params": {}, "rationale": ""}})
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "code-owner")
    client = TestClient(make_app(tmp_path))
    for evidence in (False, True):
        created = client.post("/api/runs/code/reviews", headers={"X-LoopLab-Token": "code-owner"},
            json={"include_evidence": evidence, "ttl_seconds": 3600})
        assert created.status_code == 200
        header = {"X-LoopLab-Review": created.json()["token"]}
        response = client.get("/api/review/nodes/3", headers=header)
        if not evidence:
            assert response.status_code == 403
        else:
            assert response.status_code == 200
            assert response.json()["parent_edit"]["files"]["secret.py"] != f"token='{secret}'\n"
            assert secret not in response.text
            assert response.json()["parent_edit"]["node_id"] == 2
        summary = client.get("/api/review/state", headers=header)
        assert summary.status_code == 200
        assert "parent_edit" not in summary.text
        assert secret not in summary.text
