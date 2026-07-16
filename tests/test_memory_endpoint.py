"""GET /api/memory must NOT be swallowed by the /api/{kind} catch-all (it was declared after it and 404'd
"unknown kind" the whole time, leaving the Memory panel silently empty). Regression: it returns the split
tiers, and /api/knowledge still resolves via the authoring catch-all."""
from __future__ import annotations

import orjson
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.server import make_app  # noqa: E402


def test_memory_endpoint_not_shadowed_by_kind(tmp_path):
    c = TestClient(make_app(tmp_path))
    r = c.get("/api/memory")
    assert r.status_code == 200, "memory must resolve, not 404 as an 'unknown kind'"
    body = r.json()
    assert set(("dir", "cases", "lessons", "notes")) <= set(body)   # the split tiers are present


def test_knowledge_kind_still_resolves(tmp_path):
    c = TestClient(make_app(tmp_path))
    r = c.get("/api/knowledge")
    assert r.status_code == 200 and "files" in r.json()             # served by the authoring catch-all


def test_saved_memory_dir_drives_memory_and_atlas_reads(tmp_path, monkeypatch):
    # A UI-saved global default must govern the owner read surfaces immediately; requiring the operator to
    # duplicate it in the process environment made Settings appear successful while Atlas stayed empty.
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", "")
    memory_dir = tmp_path / "portfolio-memory"
    memory_dir.mkdir()
    (memory_dir / "lessons.jsonl").write_bytes(orjson.dumps({
        "statement": "saved memory is visible",
        "outcome": "supported",
        "evidence": [1],
        "run_id": "prior-run",
        "task_id": "task-a",
    }) + b"\n")
    client = TestClient(make_app(tmp_path / "runs"))

    saved = client.put("/api/settings", json={"settings": {"memory_dir": str(memory_dir)}})
    assert saved.status_code == 200

    memory = client.get("/api/memory")
    assert memory.status_code == 200
    assert memory.json()["dir"] == str(memory_dir)
    assert memory.json()["lessons"][0]["statement"] == "saved memory is visible"

    atlas = client.get("/api/cross-run/atlas")
    assert atlas.status_code == 200
    assert atlas.json()["n_claims"] == 1 and atlas.json()["n_runs"] == 1
