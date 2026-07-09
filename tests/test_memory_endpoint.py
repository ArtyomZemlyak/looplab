"""GET /api/memory must NOT be swallowed by the /api/{kind} catch-all (it was declared after it and 404'd
"unknown kind" the whole time, leaving the Memory panel silently empty). Regression: it returns the split
tiers, and /api/knowledge still resolves via the authoring catch-all."""
from __future__ import annotations

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


def test_memory_endpoint_splits_dev_lessons(tmp_path, monkeypatch):
    """dev_lessons.jsonl must land in the `dev_lessons` bucket, NOT the generic `lessons` bucket — the
    filename also matches 'lesson', so the order-sensitive split (checked in misc.py) is load-bearing."""
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path))
    (tmp_path / "lessons.jsonl").write_text(
        '{"statement": "researcher lesson", "outcome": "supported"}\n', encoding="utf-8")
    (tmp_path / "dev_lessons.jsonl").write_text(
        '{"statement": "dev lesson", "outcome": "technique", "source": "developer"}\n', encoding="utf-8")
    body = TestClient(make_app(tmp_path)).get("/api/memory").json()
    assert "dev_lessons" in body
    assert any(l.get("statement") == "dev lesson" for l in body["dev_lessons"])
    assert all(l.get("statement") != "dev lesson" for l in body["lessons"])       # not misrouted
    assert any(l.get("statement") == "researcher lesson" for l in body["lessons"])
