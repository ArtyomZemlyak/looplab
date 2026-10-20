"""The composite MCP operations (doc 81) driven through the REAL routes: `submit_experiment`,
`finish_run`, `retry_node`, `upload_per_query` and `campaign_supertask` — each one call for what
an external agent used to script as ~11, stopping at the first failed step and replaying on the
same key."""
from __future__ import annotations

import json
import time

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.core.npyio import write_npy, write_npz
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.harness import composite
from looplab.harness.mcp_server import HarnessAPI, build_server
from looplab.serve.protocol import COMMAND_TERMINAL_STATUSES
from looplab.serve.run_commands import RunCommandService
from looplab.serve.server import make_app

OWNER = {"X-LoopLab-Token": "owner-secret"}
_NO_OBLIGATIONS = {"deep_research_every": -1, "report_every": 0, "track_hypotheses": False,
                   "concept_pivot": False, "concept_run_base": False, "cross_run_concepts": False,
                   "novelty_mode": "off", "foresight": False}
_OPEN: list = []


def _setup(tmp_path, monkeypatch, run_id="camp"):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    root = tmp_path / "runs"
    rd = root / run_id
    rd.mkdir(parents=True)
    settings = Settings(backend="toy", external_harness=True, **_NO_OBLIGATIONS)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    (rd / "task.snapshot.json").write_text(json.dumps({"kind": "quadratic"}))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "run_uid": "uid-1", "task_id": "task",
                                 "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(1)"})
    store.append("node_failed", {"node_id": 0, "error": "setup: FileNotFoundError"})
    store.append("pause", {})
    app = make_app(root)
    srv = app.state.looplab
    spawns: list = []
    srv.commands = RunCommandService(
        srv, spawn_engine=lambda args, env=None, run_dir=None: spawns.append(list(args)) or None,
        startup_timeout=0.2, command_timeout=0.4, poll_interval=0.01, max_observation_timeout=0.8)
    server = TestClient(app)
    _OPEN.append((server, rd))

    def forward(request):
        reply = server.request(request.method, request.url.raw_path.decode(), content=request.content,
                               headers={k: v for k, v in request.headers.items()
                                        if k.lower() in ("content-type", "x-looplab-token",
                                                         "idempotency-key")})
        return httpx.Response(reply.status_code, content=reply.content,
                              headers={"content-type": reply.headers.get("content-type", "")})
    api = HarnessAPI("http://testserver/", "agent-secret", transport=httpx.MockTransport(forward))
    return root, rd, store, api, server, spawns


@pytest.fixture(autouse=True)
def _settle_commands():
    yield
    try:
        for server, rd in _OPEN:
            deadline = time.time() + 15.0
            for record in sorted((rd / ".commands").glob("cmd_*.json")):
                while time.time() < deadline:
                    status = server.get(f"/api/runs/{rd.name}/commands/{record.stem}",
                                        headers=OWNER).json().get("status")
                    if status in COMMAND_TERMINAL_STATUSES:
                        break
                    time.sleep(0.02)
    finally:
        _OPEN.clear()


def _types(store):
    return [e.type for e in store.read_all()]


EXPERIMENT = {"experiment_id": "E1", "hypothesis": "x=3 beats x=0",
              "arms": {"base": {"node_id": 0}, "new": {}}, "tests": [{"label": "t"}],
              "metrics": [{"name": "m", "kind": "mean"}],
              "contrasts": [{"treatment": "new", "control": "base"}]}


def test_the_campaign_tools_are_advertised():
    pytest.importorskip("mcp")
    api = HarnessAPI("http://127.0.0.1:8765", transport=httpx.MockTransport(
        lambda request: pytest.fail("listing local tools contacted the UI")))
    names = {tool.name for tool in anyio.run(build_server(api).list_tools)}
    assert {"amend_protocol", "run_protocol", "upload_per_query", "compare_experiment",
            "shared_test_leaderboard", "campaign_supertask", "campaign_report",
            "publish_campaign_narrative", "submit_experiment", "finish_run",
            "retry_node", "code_worktree", "code_worktrees", "code_overlay"} <= names
    schemas = {tool.name: tool.model_dump(by_alias=True)["inputSchema"]["properties"]
               for tool in anyio.run(build_server(api).list_tools)}
    # Worktree + merge is the default submission path (AGENTS.md "Code changes belong in the run").
    assert {"code_from", "parent_ids"} <= set(schemas["submit_experiment"])
    assert "path" in schemas["code_worktree"] and "git" in schemas["code_overlay"]


def test_submit_experiment_preregisters_files_and_injects_in_one_call(tmp_path, monkeypatch):
    root, rd, store, api, server, spawns = _setup(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    idea = {"operator": "improve", "rationale": "move x", "concepts": ["param/x"]}
    question = {"id": "q-x", "statement": "Where is the optimum of x?", "concepts": ["param/x"]}
    dry = composite.submit_experiment(api, "camp", generation, key="E1", idea=idea,
                                      code="print(3)", research_question=question,
                                      experiment=EXPERIMENT, dry_run=True)
    assert dry["ok"] and dry["dry_run"] and "inject_node" in dry["steps"][-1]["plan"]
    before = _types(store)
    missing = composite.submit_experiment(api, "camp", generation, key="E1", idea=idea)
    assert not missing["ok"] and any("files or code" in m for m in missing["steps"][-1]["missing"])
    assert _types(store) == before
    out = composite.submit_experiment(api, "camp", generation, key="E1", idea=idea,
                                      code="print(3)", research_question=question,
                                      experiment=EXPERIMENT, experiment_reason="plan before data")
    assert out["ok"], out
    assert [s["step"] for s in out["steps"]] == [
        "read_progress", "register_experiment", "add_research_question", "inject"]
    events = store.read_all()
    inject = next(e for e in events if e.type == "inject_node")
    assert inject.data["idea"]["parent_card_id"] == "q-x"
    assert inject.data["idea"]["concepts"] == ["param/x"]
    hyp = next(e for e in events if e.type == "hypothesis_added")
    assert hyp.data["source"] == "external_agent"
    proto = server.get("/api/runs/camp/protocol", headers=OWNER).json()
    assert proto["experiments"][0]["experiment_id"] == "E1"
    assert proto["experiments"][0]["registration"]["author"]["kind"] == "agent"
    assert spawns == []                               # a paused run's queue waits for the resume
    # The same key resumes: every completed step replays, nothing new is appended.
    count = len(store.read_all())
    again = composite.submit_experiment(api, "camp", generation, key="E1", idea=idea,
                                        code="print(3)", research_question=question,
                                        experiment=EXPERIMENT, experiment_reason="plan before data")
    assert again["ok"], again
    assert len(store.read_all()) == count


def test_retry_node_is_a_new_attempt_of_the_same_node(tmp_path, monkeypatch):
    root, rd, store, api, server, spawns = _setup(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    out = composite.retry_node(api, "camp", generation, node_id=0, key="retry")
    assert any(s["step"] == "reset" for s in out["steps"]), out
    reset = [e for e in store.read_all() if e.type == "node_reset"]
    assert reset and reset[0].data["node_id"] == 0 and reset[0].data["generation"] == 0
    from looplab.events.replay import fold
    state = fold(store.read_all())
    assert set(state.nodes) == {0} and state.nodes[0].attempt == 1
    assert any(e.type == "node_failed" for e in store.read_all())     # attempt 0 keeps its record


def test_upload_compare_campaign_and_finish(tmp_path, monkeypatch):
    root, rd, store, api, server, spawns = _setup(tmp_path, monkeypatch)
    store.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(2)"})
    store.append("node_evaluated", {"node_id": 1, "metric": 0.5})
    store.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(3)"})
    store.append("node_evaluated", {"node_id": 2, "metric": 0.6})
    generation = run_generation_token(store.read_all())
    experiment = {**EXPERIMENT, "arms": {"base": {"node_id": 1}, "new": {"node_id": 2}}}
    r = api.request("POST", "/api/runs/camp/protocol/amendments", {
        "expected_generation": generation, "action_id": "e1", "kind": "experiment",
        "reason": "registered before upload", "experiment": experiment})
    assert r["status"] == 200, r
    keys = [f"q{i}" for i in range(40)]
    users = [f"u{i // 2}" for i in range(40)]
    for node, values in ((1, [0.5] * 40), (2, [0.6 if i % 3 else 0.5 for i in range(40)])):
        path = tmp_path / f"n{node}.npz"
        path.write_bytes(write_npz({"keys": write_npy(keys, dtype="<U4"),
                                    "users": write_npy(users, dtype="<U4"),
                                    "m": write_npy(values, dtype="<f8")}))
        up = composite.upload_per_query(api, "camp", generation, node_id=node, path=str(path),
                                        metrics={"m": {"kind": "mean", "array": "m"}},
                                        action_id=f"up{node}", test_label="t")
        assert up["status"] == 200, up
    comp = api.request("GET", "/api/runs/camp/experiments/E1/comparison")
    row = comp["body"]["contrasts"][0]
    assert row["status"] == "computed" and row["delta"] > 0 and row["clusters"] == 20
    grouped = composite.campaign_supertask(api, "campaign A", ["camp"])
    assert grouped["ok"], grouped
    again = composite.campaign_supertask(api, "campaign A", ["camp"])
    assert again["supertask_id"] == grouped["supertask_id"]
    assert again["steps"][-1]["status"] == "already assigned"
    report = api.request("GET", f"/api/supertasks/{grouped['supertask_id']}/campaign")
    assert report["body"]["runs"][0]["experiments"][0]["contrasts"][0]["status"] == "computed"
    dry = composite.finish_run(api, "camp", generation, key="fin", dry_run=True)
    assert dry["ok"] and dry["steps"][-1]["plan"] == ["finalize"]
    out = composite.finish_run(api, "camp", generation, key="fin",
                               report={"headline": "new beats base",
                                       "verdict": "E1 supported on the shared test"})
    assert [s["step"] for s in out["steps"]][:2] == ["read_progress", "final_report"]
    assert out["steps"][1]["ok"]
    assert "report_generated" in _types(store)
    assert any(s["step"] == "finalize" for s in out["steps"])


def test_retry_node_repeated_after_the_reset_landed_replays_instead_of_resetting_again(
        tmp_path, monkeypatch):
    """Review 2026-10-15: the inferred-generation step key read the attempt counter the reset
    itself moves, so the same call repeated after a lost response refused or reset AGAIN."""
    root, rd, store, api, server, spawns = _setup(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    # (No engine acknowledges the intent in this harness, so the command itself settles
    # `timed_out` after its fold — the reset row is written either way.)
    first = composite.retry_node(api, "camp", generation, node_id=0, key="retry")
    assert first["steps"][-1]["step"] == "reset", first
    assert [e.type for e in store.read_all()].count("node_reset") == 1
    again = composite.retry_node(api, "camp", generation, node_id=0, key="retry")
    assert [s["step"] for s in again["steps"]] == ["reset"], again       # no read_node: replay
    assert again["steps"][0].get("replayed") is True
    assert again["steps"][0].get("id") == first["steps"][-1].get("id")
    assert [e.type for e in store.read_all()].count("node_reset") == 1


def test_a_reused_experiment_id_with_another_declaration_stops_before_inject(tmp_path, monkeypatch):
    """Review 2026-10-15 (weekly-registration-conflict): `experiment_exists` is not a replay
    receipt. A second candidate under the SAME declaration proceeds; a changed hypothesis or
    correction under a reused id stops before anything is injected."""
    root, rd, store, api, server, spawns = _setup(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    idea = {"operator": "improve", "rationale": "move x"}
    first = composite.submit_experiment(api, "camp", generation, key="K1", idea=idea,
                                        code="print(3)", experiment=EXPERIMENT)
    assert first["ok"], first
    injects = lambda: sum(1 for e in store.read_all() if e.type == "inject_node")
    assert injects() == 1
    changed = {**EXPERIMENT, "hypothesis": "x=0 beats x=3", "correction": "none"}
    clash = composite.submit_experiment(api, "camp", generation, key="K2", idea=idea,
                                        code="print(4)", experiment=changed)
    assert not clash["ok"] and clash["stopped_at"] == "register_experiment", clash
    assert injects() == 1
    proto = server.get("/api/runs/camp/protocol", headers=OWNER).json()
    assert proto["experiments"][0]["hypothesis"] == "x=3 beats x=0"
    second = composite.submit_experiment(api, "camp", generation, key="K3", idea=idea,
                                         code="print(5)", experiment=EXPERIMENT)
    assert second["ok"], second
    assert injects() == 2


def test_submit_experiment_forwards_artifact_kind_and_evaluated_uses(tmp_path, monkeypatch):
    root, rd, store, api, server, spawns = _setup(tmp_path, monkeypatch)
    store.append('node_created', {'node_id': 1, 'parent_ids': [], 'operator': 'draft',
                                  'idea': {'operator': 'draft'}, 'code': 'x'})
    store.append('node_evaluated', {'node_id': 1, 'metric': 0.5})
    settings = Settings(backend='toy', external_harness=True,
                        **{**_NO_OBLIGATIONS, 'novelty_mode': 'algo'})
    (rd / 'config.snapshot.json').write_text(settings.model_dump_json())
    generation = run_generation_token(store.read_all())
    idea = {'operator': 'inject', 'rationale': 'prepare reusable arrays'}
    prepared = composite.submit_experiment(api, 'camp', generation, key='prepare', idea=idea,
                                           code='print(1)', node_kind='artifact', uses=[1])
    assert prepared['ok'], prepared
    injected = next(e for e in store.read_all() if e.type == 'inject_node')
    assert injected.data['node_kind'] == 'artifact'
    assert injected.data['uses'] == [1] and injected.data['uses_attempts'] == {'1': 0}
    ordinary = composite.submit_experiment(api, 'camp', generation, key='ordinary', idea=idea,
                                          code='print(2)')
    assert not ordinary['ok']
    assert 'novelty' in str(ordinary)
