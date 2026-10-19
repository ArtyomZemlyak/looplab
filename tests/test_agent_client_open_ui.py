"""doc 82 П5: on the OPEN UI the agent names itself, and is attributed and fenced as the agent.

`harness-mcp` sends `X-LoopLab-Client: harness-mcp/<version>` (П1). Before this the open UI could not
tell the agent from the browser: its commands carried no `submitted_by`, its activity was never
observed, and the scoped token's fences did not apply. Decision Р5: the header carries the agent
SCOPE (the token's deny list and internal-run refusals) and its attribution, except that it may
launch its own EXTERNAL run (MCP `start_run`). It is a guard for an honest agent, never protection —
any caller can send or omit the header — so a request without it behaves exactly as before.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.serve.protocol import COMMAND_TERMINAL_STATUSES
from looplab.serve.run_commands import RunCommandService
from looplab.serve.server import agent_write_refused, make_app
from tests.factories import http_run_generation, post_command

AGENT = {"X-LoopLab-Client": "harness-mcp/0.1.0"}
_NO_OBLIGATIONS = {"deep_research_every": -1, "report_every": 0, "track_hypotheses": False,
                   "concept_pivot": False, "concept_run_base": False, "cross_run_concepts": False,
                   "strategist_every": 0, "novelty_mode": "off"}


def _client(tmp_path, monkeypatch, *, external: bool):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    settings = Settings(backend="toy", external_harness=external,
                        **(_NO_OBLIGATIONS if external else {}))
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    (rd / "task.snapshot.json").write_text(json.dumps({"kind": "quadratic"}))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "one", "task_id": "task",
                                 "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(1)"})
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0})
    store.append("pause", {})
    app = make_app(tmp_path)
    srv = app.state.looplab
    srv.commands = RunCommandService(
        srv, spawn_engine=lambda args, env=None, run_dir=None: None,
        startup_timeout=0.2, command_timeout=0.4, poll_interval=0.01, max_observation_timeout=0.8)
    client = TestClient(app)
    client.rd, client.srv = rd, srv
    return client


def _settle(client, record):
    deadline = time.time() + 10
    while record.get("status") not in COMMAND_TERMINAL_STATUSES and time.time() < deadline:
        time.sleep(0.02)
        record = client.get(f"/api/runs/demo/commands/{record['id']}").json()
    return record


def test_the_open_ui_agent_is_attributed_and_the_browser_is_not(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, external=True)
    agent = _settle(client, post_command(client, "annotation", {"node_id": 0, "text": "agent"},
                                         key="a1", headers=AGENT).json())
    browser = _settle(client, post_command(client, "annotation", {"node_id": 0, "text": "me"},
                                           key="b1").json())
    assert agent["status"] == "succeeded" and browser["status"] == "succeeded"
    records = {p.stem: json.loads(p.read_text()) for p in (client.rd / ".commands").glob("cmd_*.json")}
    by_id = {r["id"]: r for r in records.values()}
    assert by_id[agent["id"]]["submitted_by"] == "agent_client"
    assert "submitted_by" not in by_id[browser["id"]]
    # Activity: the agent's requests count, the browser's never do (doc 78 §4).
    generation = http_run_generation(client)
    snap = client.srv.agent_activity.snapshot(client.srv.run_dir("demo"), generation)
    assert snap["status"] == "recent_request"
    assert client.srv.agent_activity.seen_since("demo", 0.0)


def test_the_open_ui_agent_meets_the_token_fences_and_the_browser_does_not(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, external=False)
    # The deny list: operator defaults, the harness policy, the owner's skills.
    for method, path, body in (("put", "/api/settings", {"max_nodes": 3}),
                               ("put", "/api/runs/demo/harness-policy", {"level": "none"}),
                               ("put", "/api/skills/x", {"content": "y"})):
        refused = getattr(client, method)(path, json=body, headers=AGENT)
        assert refused.status_code == 403 and refused.json()["code"] == "agent_scope_refused", path
    assert client.put("/api/settings", json={"max_nodes": 3}).status_code == 200
    # An INTERNAL run's paid intents are refused to the agent, as to the token (doc 70 70.8).
    fork = _settle(client, post_command(client, "fork", {"from_node_id": 0}, key="f1",
                                        headers=AGENT).json())
    assert fork["status"] == "rejected" and fork["error"]["code"] == "agent_token_refused"
    owner_fork = post_command(client, "fork", {"from_node_id": 0}, key="f2")
    assert owner_fork.status_code == 200 and owner_fork.json()["status"] != "rejected"
    _settle(client, owner_fork.json())
    # The operator's hook stays the operator's.
    hook = client.put("/api/agent-notify", json={"command": ["true"]}, headers=AGENT)
    assert hook.status_code == 403
    # Reads are untouched.
    assert client.get("/api/runs/demo/state", headers=AGENT).status_code == 200


def _repo_task(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "test.py").write_text("print('{}')\n", encoding="utf-8")
    return {"goal": "maximize recall", "direction": "max", "repo": str(repo),
            "cmd": {"command": ["python", "test.py"],
                    "metric": {"reader": "stdout_json", "key": "recall"}}}


def test_the_open_ui_agent_launches_only_an_external_run(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path / "runs"))
    task = _repo_task(tmp_path)
    internal = client.post("/api/start/preflight", json={"run_id": "x", "task": task}, headers=AGENT)
    assert internal.status_code == 403
    assert internal.json()["detail"]["code"] == "agent_scope_refused"
    verdict = client.post("/api/validate", json={"run_id": "x", "task": task}, headers=AGENT)
    assert verdict.status_code == 403
    external = client.post("/api/start/preflight", headers=AGENT, json={
        "run_id": "x", "task": task, "settings": {"external_harness": True}})
    assert external.status_code == 200, external.text
    # The browser's own launch is unchanged, and genesis stays refused to the agent.
    assert client.post("/api/start/preflight", json={"run_id": "x", "task": task}).status_code == 200
    assert client.post("/api/genesis", json={"instruction": "x"}, headers=AGENT).status_code == 403


def test_a_token_server_ignores_the_header_and_still_refuses_the_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "operator-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    client = TestClient(make_app(tmp_path / "runs"))
    task = _repo_task(tmp_path)
    assert client.post("/api/start/preflight", headers=AGENT, json={
        "run_id": "x", "task": task, "settings": {"external_harness": True}}).status_code == 401
    token = client.post("/api/start/preflight", json={
        "run_id": "x", "task": task, "settings": {"external_harness": True}},
        headers={**AGENT, "X-LoopLab-Token": "agent-secret"})
    assert token.status_code == 403                     # doc 82 Р3: a token never launches


@pytest.mark.parametrize("method,path,refused", [
    ("POST", "/api/start", True), ("POST", "/api/start/preflight", True),
    ("POST", "/api/start/x/resolve-claim", True), ("POST", "/api/genesis", True),
    ("PUT", "/api/settings", True), ("GET", "/api/settings", False),
    ("POST", "/api/runs/r/commands", False), ("DELETE", "/api/runs/r", True),
])
def test_the_one_deny_list(method, path, refused):
    assert agent_write_refused(method, path) is refused
    launch = path in ("/api/start", "/api/start/preflight")
    assert agent_write_refused(method, path, external_launch=True) is (refused and not launch)


def test_agent_added_nodes_count_both_agent_parties(tmp_path, monkeypatch):
    """The operator's node cap binds the agent whichever way it connected (doc 81 policy)."""
    from looplab.harness.campaign import agent_added_nodes
    client = _client(tmp_path, monkeypatch, external=True)
    rec = _settle(client, post_command(client, "budget_extend", {"add_nodes": 2}, key="n1",
                                       headers=AGENT).json())
    assert rec["status"] != "rejected", rec
    assert agent_added_nodes(client.srv, client.rd) == 2


def test_pause_and_resume_carry_the_agents_reason_and_fold_as_before(tmp_path, monkeypatch):
    """doc 82 П5: `reason` rides on pause/resume; a log without it folds exactly as before, and a
    resume's reason changes no state."""
    from looplab.events.replay import fold
    client = _client(tmp_path, monkeypatch, external=True)
    refused = post_command(client, "pause", {"reason": "infra_unavailable"}, key="p1", headers=AGENT)
    # An engine code is never stored: the intake rejects the command (a durable rejected record).
    assert refused.status_code == 200 and refused.json()["status"] == "rejected", refused.json()
    assert "infra_unavailable" not in [e.data.get("reason") for e in
                                       EventStore(client.rd / "events.jsonl").read_all()]
    resumed = _settle(client, post_command(client, "resume", {"reason": "GPU is free again"},
                                           key="r1", headers=AGENT).json())
    assert resumed["status"] not in ("rejected",), resumed
    events = EventStore(client.rd / "events.jsonl").read_all()
    resume = next(e for e in events if e.type == "resume")
    assert resume.data["reason"] == "GPU is free again"

    plain = [("run_started", {"run_id": "x", "task_id": "t", "goal": "g", "direction": "min"}),
             ("pause", {}), ("resume", {})]
    worded = [("run_started", plain[0][1]), ("pause", {"reason": "lunch"}),
              ("resume", {"reason": "back"})]

    def state(rows, name):
        (tmp_path / name).mkdir(exist_ok=True)
        store = EventStore(tmp_path / name / "events.jsonl")
        for kind, data in rows:
            store.append(kind, data)
        return fold(store.read_all())

    a, b = state(plain, "plain"), state(worded, "worded")
    assert (a.paused, a.pause_reason) == (b.paused, None) == (False, None)
    assert a.model_dump(exclude={"pause_reason"}) == b.model_dump(exclude={"pause_reason"})
