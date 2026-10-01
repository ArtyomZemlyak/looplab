"""Connection context is an authenticated, fenced read, not a credential export."""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote

import pytest
import httpx
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.server import make_app


def _run(tmp_path, monkeypatch, *, external=True, credential=True, repo=False, run_id="demo"):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "operator-secret-1234")
    if credential:
        monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret-1234")
    else:
        monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    rd = tmp_path / "runs" / run_id
    rd.mkdir(parents=True)
    (rd / "config.snapshot.json").write_text(json.dumps(Settings(
        backend="toy", external_harness=external, memory_dir=str(tmp_path / "memory")
    ).model_dump(mode="json")))
    task = json.loads((Path(__file__).resolve().parents[1] / "examples/toy_task.json").read_text())
    if repo:
        source = tmp_path / "repo-agent-secret-1234"
        source.mkdir()
        (source / "score.py").write_text("print('metric=1')")
        task = {"kind": "repo", "id": "task", "goal": "operator-secret-1234",
                "editable_path": str(source), "edit_surface": [f"p{i}.py" for i in range(50)],
                "protect": ["score.py"], "eval": {"command": ["python", "score.py"]}}
    (rd / "task.snapshot.json").write_text(json.dumps(task))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "run_uid": "incarnation-one",
                                 "task_id": "task", "goal": "operator-secret-1234", "direction": "min"})
    return rd, store, TestClient(make_app(rd.parent))


@pytest.mark.parametrize("repo", [False, True])
def test_handoff_has_actual_server_paths_constraints_and_no_secrets(tmp_path, monkeypatch, repo):
    rd, store, client = _run(tmp_path, monkeypatch, repo=repo)
    generation = run_generation_token(store.read_all())
    path = "/api/runs/demo/harness-handoff"
    args = {"expected_generation": generation}
    assert client.get(path, params=args).status_code == 401
    before = {p.name: p.read_bytes() for p in rd.iterdir() if p.is_file()}
    for token in ("operator-secret-1234", "agent-secret-1234"):
        response = client.get(path, params=args, headers={"X-LoopLab-Token": token})
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["generation"] == generation and value["run_uid"] == "incarnation-one"
        assert value["server_paths"]["run_dir"] == str(rd.resolve())
        assert value["server_paths"]["run_root"] == str(rd.parent.resolve())
        assert value["credential_configured"] and value["agent_connection"] == "not_measured"
        assert value["engine_running"] is False
        assert "operator-secret-1234" not in response.text and "agent-secret-1234" not in response.text
        assert response.headers["Cache-Control"] == "no-store"
        if repo:
            assert value["workspace"]["kind"] == "repository"
            assert value["workspace"]["edit_surface"]["total"] == 50
            assert value["workspace"]["edit_surface"]["truncated"]
            assert "score.py" in value["workspace"]["protected_names"]["items"]
            assert "REDACTED" in value["workspace"]["source_paths"]["items"][0]
        else:
            assert value["workspace"]["kind"] == "script"
            assert value["workspace"]["source_paths"]["items"] == []
    assert {p.name: p.read_bytes() for p in rd.iterdir() if p.is_file()} == before


def test_handoff_refuses_stale_internal_incomplete_and_missing_snapshots(tmp_path, monkeypatch):
    rd, store, client = _run(tmp_path, monkeypatch)
    path = "/api/runs/demo/harness-handoff"
    generation = run_generation_token(store.read_all())
    headers = {"X-LoopLab-Token": "operator-secret-1234"}
    assert client.get(path, params={"expected_generation": "bad"}, headers=headers).status_code == 400
    assert client.get(path, params={"expected_generation": "0" * 64}, headers=headers).status_code == 409
    args = {"expected_generation": generation}
    task = rd / "task.snapshot.json"
    saved = task.read_bytes()
    task.write_bytes(b"not json")
    assert client.get(path, params=args, headers=headers).status_code == 503
    task.write_bytes(saved)
    config = json.loads((rd / "config.snapshot.json").read_text())
    config["external_harness"] = False
    (rd / "config.snapshot.json").write_text(json.dumps(config))
    assert client.get(path, params=args, headers=headers).status_code == 409
    with (rd / "events.jsonl").open("ab") as fh:
        fh.write(b"damaged tail\n")
    assert client.get(path, params=args, headers=headers).status_code == 409


def test_handoff_reports_missing_scoped_credential_without_exporting_owner(tmp_path, monkeypatch):
    _, store, client = _run(tmp_path, monkeypatch, credential=False)
    response = client.get("/api/runs/demo/harness-handoff", params={
        "expected_generation": run_generation_token(store.read_all())},
        headers={"X-LoopLab-Token": "operator-secret-1234"})
    assert response.status_code == 200
    assert response.json()["credential_configured"] is False
    assert "operator-secret-1234" not in response.text


def test_handoff_and_progress_route_a_literal_encoded_looking_run_id(tmp_path, monkeypatch):
    run_id = "mnist # %2F"
    rd, store, client = _run(tmp_path, monkeypatch, run_id=run_id)
    params = {"expected_generation": run_generation_token(store.read_all())}
    headers = {"X-LoopLab-Token": "agent-secret-1234"}
    path = f"/api/runs/{quote(run_id, safe='')}"
    async def read():
        # Starlette TestClient unquotes httpx's already-decoded path again. ASGITransport
        # preserves the literal %2F, matching uvicorn's single decode of raw_path.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                     base_url="http://127.0.0.1") as transport:
            response = await transport.get(path + "/harness-handoff", params=params, headers=headers)
            progress = await transport.get(path + "/harness-progress", params=params, headers=headers)
            return response, progress
    response, progress = asyncio.run(read())
    assert response.status_code == 200
    assert response.json()["run_id"] == run_id
    assert response.json()["server_paths"]["run_dir"] == str(rd.resolve())
    assert progress.status_code == 200


def test_handoff_unreadable_event_log_is_a_coded_refusal(tmp_path, monkeypatch):
    _, store, client = _run(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    monkeypatch.setattr("looplab.harness.handoff.log_integrity", lambda _: {
        "complete": False, "unreadable": True})
    response = client.get("/api/runs/demo/harness-handoff", params={
        "expected_generation": generation}, headers={"X-LoopLab-Token": "operator-secret-1234"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "event_log_unreadable"
    assert "operator-secret-1234" not in response.text


def test_observe_only_state_skips_pending_reset_reconciliation(tmp_path, monkeypatch):
    rd, store, client = _run(tmp_path, monkeypatch)
    before = {p.name: p.read_bytes() for p in rd.iterdir() if p.is_file()}
    monkeypatch.setattr("looplab.serve.routers.runs.load_run_reset_marker",
                        lambda *_: pytest.fail("diagnostic read entered reset reconciliation"))
    monkeypatch.setattr("looplab.serve.routers.runs.reconcile_run_reset_observation",
                        lambda *_: pytest.fail("diagnostic read completed an operator reset"))
    response = client.get("/api/runs/demo/state", params={"observe_only": "true"},
                          headers={"X-LoopLab-Token": "agent-secret-1234"})
    assert response.status_code == 200
    assert response.json()["generation"] == run_generation_token(store.read_all())
    assert {p.name: p.read_bytes() for p in rd.iterdir() if p.is_file()} == before
