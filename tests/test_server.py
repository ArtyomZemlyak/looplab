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
