"""Live UI server (the [ui] extra). Skipped entirely when fastapi isn't installed, so the base
offline suite is unaffected. Builds a real finished run, then exercises the read API, time-travel,
node detail, the control append, and config masking through FastAPI's TestClient.
"""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.orchestrator import Engine  # noqa: E402
from looplab.policy import GreedyTree  # noqa: E402
from looplab.replay import fold  # noqa: E402
from looplab.eventstore import EventStore  # noqa: E402
from looplab.sandbox import SubprocessSandbox  # noqa: E402
from looplab.server import make_app  # noqa: E402
from looplab.toytask import ToyTask  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def _build_run(root: Path, name: str = "demo"):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(root / name, task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4))
    return anyio.run(eng.run)


def test_runs_list_state_and_node_detail(tmp_path):
    st = _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))

    runs = client.get("/api/runs").json()
    assert any(r["run_id"] == "demo" and r["finished"] for r in runs)

    payload = client.get("/api/runs/demo/state").json()
    assert payload["state"]["finished"] is True
    assert len(payload["state"]["nodes"]) == len(st.nodes)
    assert payload["seq"] >= 0
    # heavy fields trimmed out of the live state
    any_node = next(iter(payload["state"]["nodes"].values()))
    assert "code" not in any_node

    # node detail carries the full code + a trace block
    nid = st.best().id
    node = client.get(f"/api/runs/demo/nodes/{nid}").json()
    assert node["id"] == nid and "code" in node and "trace" in node


def test_time_travel_seq(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    # seq=0 is just run_started -> no nodes yet; the full state has nodes.
    early = client.get("/api/runs/demo/state", params={"seq": 0}).json()
    full = client.get("/api/runs/demo/state").json()
    assert len(early["state"]["nodes"]) == 0
    assert len(full["state"]["nodes"]) > 0


def test_control_append_and_validation(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}})
    assert r.status_code == 200 and r.json()["type"] == "pause"
    st = fold(EventStore(tmp_path / "demo" / "events.jsonl").read_all())
    assert st.paused is True
    # unknown control event rejected
    bad = client.post("/api/runs/demo/control", json={"type": "danger", "data": {}})
    assert bad.status_code == 400


def test_config_masked_and_gpu_softfail(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    cfg = client.get("/api/runs/demo/config").json()
    # never leak a real secret value
    assert cfg.get("llm_api_key") in (None, "***")
    gpu = client.get("/api/gpu").json()
    assert "available" in gpu  # True or False, never an error


def test_settings_get_put_roundtrip(tmp_path):
    client = TestClient(make_app(tmp_path))
    base = client.get("/api/settings").json()
    assert "settings" in base and "defaults" in base and base["overrides"] == {}
    # saving a value EQUAL to the default keeps the override file empty (stores only diffs)
    default_nodes = base["defaults"]["max_nodes"]
    client.put("/api/settings", json={"settings": {"max_nodes": default_nodes}})
    assert client.get("/api/settings").json()["overrides"] == {}
    # a real change is persisted and reflected in the resolved settings
    r = client.put("/api/settings", json={"settings": {"max_nodes": 99, "policy": "mcts"}}).json()
    assert r["overrides"] == {"max_nodes": 99, "policy": "mcts"}
    got = client.get("/api/settings").json()
    assert got["settings"]["max_nodes"] == 99 and got["settings"]["policy"] == "mcts"
    # secrets are never accepted as an override
    client.put("/api/settings", json={"settings": {"llm_api_key": "leak"}})
    assert "llm_api_key" not in client.get("/api/settings").json()["overrides"]


def test_tasks_catalogue(tmp_path):
    client = TestClient(make_app(tmp_path))
    tasks = client.get("/api/tasks").json()["tasks"]
    assert any(t["name"] == "toy_task.json" and t["goal"] for t in tasks)


def test_start_validation_and_env(tmp_path, monkeypatch):
    import looplab.server as server
    spawned = {}

    def fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        spawned["env"] = kw.get("env", {})
        class _P:  # noqa: D401 - stub
            pass
        return _P()
    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)

    client = TestClient(make_app(tmp_path))
    # missing fields / nonexistent task -> 400
    assert client.post("/api/start", json={"run_id": "x"}).status_code == 400
    assert client.post("/api/start", json={"task_file": "nope.json", "run_id": "x"}).status_code == 400
    # a real task spawns the engine with per-run settings as LOOPLAB_* env
    ok = client.post("/api/start", json={
        "task_file": str(TASK), "run_id": "fromui",
        "settings": {"max_nodes": 3, "backend": "toy", "require_approval": True}})
    assert ok.status_code == 200
    assert spawned["env"]["LOOPLAB_MAX_NODES"] == "3"
    assert spawned["env"]["LOOPLAB_REQUIRE_APPROVAL"] == "true"
    assert (tmp_path / "fromui" / "ui_meta.json").exists()
    # a second start on the same id is refused once the run has events
    (tmp_path / "fromui" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    assert client.post("/api/start", json={"task_file": str(TASK), "run_id": "fromui"}).status_code == 409


def test_inject_node_control_append(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/control", json={"type": "inject_node", "data": {
        "idea": {"operator": "manual", "params": {"x": 0.5}, "rationale": "hand"}, "parent_id": None}})
    assert r.status_code == 200 and r.json()["type"] == "inject_node"
    st = fold(EventStore(tmp_path / "demo" / "events.jsonl").read_all())
    assert st.inject_requests and st.inject_requests[0]["idea"]["operator"] == "manual"


def test_chat_suggest_health_softfail(tmp_path):
    # These hit the LLM endpoint; whether or not a model is reachable they must return 200 with a
    # well-formed envelope (ok: bool) — never raise. Asserts the shape, not the model output.
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    c = client.post("/api/runs/demo/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert c.status_code == 200 and "ok" in c.json()
    s = client.post("/api/runs/demo/suggest", json={"instruction": "try a higher degree"})
    assert s.status_code == 200 and "ok" in s.json()
    h = client.get("/api/llm/health").json()
    assert "ok" in h and "model" in h


def test_chat_returns_trace_with_user_and_completion(tmp_path, monkeypatch):
    """A successful /chat reply must carry a langfuse-style `trace` whose prompt includes the user's
    ACTUAL message (not just the system prompt) plus the completion — the Dock chat-trace card depends
    on this contract, so a dropped/renamed key must fail CI."""
    _build_run(tmp_path)
    import looplab.server as server

    class _FakeClient:
        model = "fake-model"

        def complete_text(self, messages):
            return "a grounded answer"

    monkeypatch.setattr(server, "make_llm_client", lambda *a, **k: _FakeClient())
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/chat",
                    json={"messages": [{"role": "user", "content": "why did node 1 fail?"}]})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["text"] == "a grounded answer"
    tr = body["trace"]
    assert tr["model"] == "fake-model"
    assert tr["completion"] == "a grounded answer"
    assert tr["user"] == "why did node 1 fail?"          # the real input is captured in the trace
    assert tr["system"]                                   # system prompt (run/node context) present


def test_cors_is_allowlisted_not_wildcard(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    # an arbitrary web page the operator has open must NOT be allowed to drive the control-plane
    evil = client.get("/api/runs", headers={"Origin": "http://evil.example"})
    assert evil.headers.get("access-control-allow-origin") != "*"
    assert evil.headers.get("access-control-allow-origin") in (None, "")
    # the Vite dev server origin is still allowed (dev workflow preserved)
    ok = client.get("/api/runs", headers={"Origin": "http://localhost:5173"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_sse_emits_state_snapshot(tmp_path):
    _build_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    with client.stream("GET", "/api/runs/demo/events") as resp:
        assert resp.status_code == 200
        chunk = next(resp.iter_lines())
        # the very first SSE frame is an id/state/data block
        for _ in range(5):
            if "state" in chunk or "id:" in chunk:
                break
            chunk = next(resp.iter_lines())
        assert "id:" in chunk or "state" in chunk


def test_g1_auth_token_required_on_mutating(tmp_path, monkeypatch):
    """G1: with LOOPLAB_UI_TOKEN set, mutating /api/* needs the X-LoopLab-Token header; reads stay open."""
    _build_run(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "s3cret")
    client = TestClient(make_app(tmp_path))
    # reads are open
    assert client.get("/api/runs").status_code == 200
    # mutating without the token -> 401
    r = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}})
    assert r.status_code == 401
    # with the token -> allowed
    r = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}},
                    headers={"X-LoopLab-Token": "s3cret"})
    assert r.status_code == 200


def test_g1_no_token_means_open(tmp_path, monkeypatch):
    """Default (no token) -> the control plane is open, behaviour unchanged."""
    _build_run(tmp_path)
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}})
    assert r.status_code == 200
