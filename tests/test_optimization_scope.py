from __future__ import annotations

import time

from looplab.events.run_generation import run_generation_token
from looplab.harness import composite
from looplab.harness.campaign import read_policy, write_policy
from tests.test_campaign_composite import _setup


def _command(server, run_id, generation, event_type, data, key):
    return server.post(f"/api/runs/{run_id}/commands", headers={
        "X-LoopLab-Token": "agent-secret", "Idempotency-Key": key,
        "Content-Type": "application/json"},
        json={"type": event_type, "data": data, "expected_generation": generation})


def test_optimization_scope_admits_profiles_and_blocks_scientific_fit(tmp_path, monkeypatch):
    root, rd, store, api, server, spawns = _setup(tmp_path, monkeypatch)
    written = write_policy(rd, {"execution": {
        "mode": "optimization_only", "allowed_job_roles": ["profile"]}})
    assert written["execution"]["version"] == 1
    assert read_policy(rd)["execution"]["allowed_job_roles"] == ["profile"]
    generation = run_generation_token(store.read_all())
    idea = {"operator": "profile", "rationale": "measure runtime", "concepts": ["throughput"]}
    accepted = composite.submit_experiment(
        api, "camp", generation, key="profile-ok", idea=idea,
        code="print('profile')", node_kind="artifact", job_role="profile")
    assert accepted["ok"], accepted
    injected = next(e for e in reversed(store.read_all()) if e.type == "inject_node")
    assert injected.data["job_role"] == "profile" and injected.data["node_kind"] == "artifact"
    refused = composite.submit_experiment(
        api, "camp", generation, key="fit-no", idea=idea,
        code="print('fit')", node_kind="artifact", job_role="fit")
    assert not refused["ok"]
    assert "optimization_scope_refused" in str(refused)


def test_saved_fit_cannot_retry_or_resume_under_optimization_scope(tmp_path, monkeypatch):
    root, rd, store, api, server, spawns = _setup(tmp_path, monkeypatch)
    store.append("node_created", {
        "node_id": 9, "parent_ids": [], "operator": "fit",
        "idea": {"operator": "fit", "params": {}}, "code": "print('fit')", "job_role": "fit"})
    write_policy(rd, {"execution": {
        "mode": "optimization_only", "allowed_job_roles": ["profile"]}})
    generation = run_generation_token(store.read_all())
    reset = _command(server, "camp", generation, "node_reset", {
        "node_id": 9, "generation": 0, "from_stage": "eval"}, "reset-fit-no")
    assert reset.status_code == 200
    assert reset.json()["status"] == "rejected"
    assert reset.json()["error"]["code"] == "optimization_scope_refused"
    resumed = _command(server, "camp", generation, "resume", {"reason": "resume saved fit"}, "resume-fit-no")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "rejected"
    assert resumed.json()["error"]["code"] == "optimization_scope_refused"
    assert not any(e.type in ("node_reset", "resume") and e.data.get("node_id") == 9
                   for e in store.read_all())


def test_pending_profile_can_resume_under_optimization_scope(tmp_path, monkeypatch):
    root, rd, store, api, server, spawns = _setup(tmp_path, monkeypatch)
    store.append("node_created", {
        "node_id": 10, "parent_ids": [], "operator": "profile",
        "idea": {"operator": "profile", "params": {}}, "code": "print('profile')",
        "job_role": "profile"})
    write_policy(rd, {"execution": {
        "mode": "optimization_only", "allowed_job_roles": ["profile"]}})
    generation = run_generation_token(store.read_all())
    resumed = _command(server, "camp", generation, "resume", {"reason": "finish profile"}, "resume-profile")
    assert resumed.status_code == 200, resumed.text
    record = resumed.json()
    assert record["status"] != "rejected", record
    assert (record.get("error") or {}).get("code") != "optimization_scope_refused"
    for _ in range(100):
        if any(e.type == "resume" for e in store.read_all()):
            break
        time.sleep(0.01)
    assert any(e.type == "resume" for e in store.read_all())
