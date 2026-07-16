"""GET /api/memory must NOT be swallowed by the /api/{kind} catch-all (it was declared after it and 404'd
"unknown kind" the whole time, leaving the Memory panel silently empty). Regression: it returns the split
tiers, and /api/knowledge still resolves via the authoring catch-all."""
from __future__ import annotations

import json

import orjson
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.server import make_app  # noqa: E402
import looplab.serve.routers.misc as misc_router  # noqa: E402


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


def test_memory_endpoint_is_allowlisted_row_safe_and_bounded(tmp_path, monkeypatch):
    memory_dir = tmp_path / "portfolio-memory"
    memory_dir.mkdir()
    monkeypatch.setattr(misc_router, "_MEMORY_TIER_LIMIT", 3)
    monkeypatch.setattr(misc_router, "_MEMORY_SOURCE_ROWS", 20)

    cases = [
        [],
        {"task_id": "bad-params", "params": {"nested": {"too": {"deep": "value"}}}},
        *[{"task_id": f"task-{index}", "goal": "g", "direction": "min", "metric": index,
           "params": {"depth": index}} for index in range(5)],
    ]
    (memory_dir / "cases.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in cases), encoding="utf-8")
    (memory_dir / "lessons.jsonl").write_text(
        '{broken\n[]\n'
        + "".join(json.dumps({"statement": f"lesson-{index}", "run_id": f"r{index}"}) + "\n"
                  for index in range(5)), encoding="utf-8")
    (memory_dir / "meta_notes.jsonl").write_text(
        json.dumps({"task_id": "t", "note": "usable note"}) + "\n", encoding="utf-8")
    # This is an operator-governance ledger, not an episodic case tier.
    (memory_dir / "claim_decisions.jsonl").write_text(
        json.dumps({"task_id": "must-not-appear", "note": "not a case"}) + "\n", encoding="utf-8")

    client = TestClient(make_app(tmp_path / "runs"))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory_dir)}}).status_code == 200
    payload = client.get("/api/memory").json()

    assert payload["projection"] == "bounded_recent_tail"
    assert [row["task_id"] for row in payload["cases"]] == ["task-2", "task-3", "task-4"]
    assert [row["statement"] for row in payload["lessons"]] == ["lesson-2", "lesson-3", "lesson-4"]
    assert payload["notes"] == [{"note": "usable note", "task_id": "t"}]
    assert payload["page"]["truncated"] is True
    assert payload["page"]["tiers"]["lessons"]["skipped"] == 2
    assert "must-not-appear" not in json.dumps(payload)


def test_memory_endpoint_bounds_source_bytes_and_oversized_rows(tmp_path, monkeypatch):
    memory_dir = tmp_path / "portfolio-memory"
    memory_dir.mkdir()
    monkeypatch.setattr(misc_router, "_MEMORY_SOURCE_BYTES", 500)
    monkeypatch.setattr(misc_router, "_MEMORY_ROW_BYTES", 180)
    (memory_dir / "lessons.jsonl").write_text(
        "".join(json.dumps({"statement": "old-" + "x" * 80 + str(index)}) + "\n"
                  for index in range(20))
        + json.dumps({"statement": "z" * 300}) + "\n"
        + json.dumps({"statement": "recent usable"}) + "\n",
        encoding="utf-8",
    )
    client = TestClient(make_app(tmp_path / "runs"))
    assert client.put("/api/settings", json={"settings": {"memory_dir": str(memory_dir)}}).status_code == 200

    payload = client.get("/api/memory").json()
    receipt = payload["page"]["tiers"]["lessons"]

    assert payload["lessons"][-1]["statement"] == "recent usable"
    assert receipt["source_window_truncated"] is True and receipt["skipped"] >= 1
