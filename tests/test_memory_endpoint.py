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
