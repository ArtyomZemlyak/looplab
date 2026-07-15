"""PART V §22 — the cross-run HTTP surface: Research Atlas data (read) + operator claim decisions (write).

Through FastAPI's TestClient: GET atlas/claims read the portfolio; POST claim-decide is the operator
governance write, honored on the next read (rejected → maturity operator-rejected). Agents never use this
router — it is the human/UI surface (§22.4).
"""
from __future__ import annotations

import os
from pathlib import Path

import orjson
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.server import make_app  # noqa: E402


def _seed_memory(statement="hard-neg helps"):
    md = Path(os.environ["LOOPLAB_MEMORY_DIR"])   # conftest points this at a tmp dir
    md.mkdir(parents=True, exist_ok=True)
    (md / "lessons.jsonl").write_bytes(orjson.dumps(
        {"statement": statement, "outcome": "supported", "evidence": [1],
         "run_id": "r1", "task_id": "t"}) + b"\n")
    return md


def test_claims_and_atlas_read(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    r = client.get("/api/cross-run/claims")
    assert r.status_code == 200
    assert any("hard-neg helps" in c["statement"] for c in r.json()["claims"])
    a = client.get("/api/cross-run/atlas")
    assert a.status_code == 200 and a.json()["n_claims"] >= 1


def test_operator_decision_write_is_honored(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/cross-run/claim-decide",
                    json={"statement": "hard-neg helps", "decision": "rejected", "note": "overruled"})
    assert r.status_code == 200 and r.json()["ok"]
    # the next read reflects the operator verdict
    claims = client.get("/api/cross-run/claims").json()["claims"]
    c = [x for x in claims if "hard-neg helps" in x["statement"]][0]
    assert c["maturity"] == "operator-rejected"


def test_invalid_decision_is_400(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/cross-run/claim-decide", json={"statement": "x", "decision": "bogus"})
    assert r.status_code == 400


def test_contested_filter(tmp_path):
    md = Path(os.environ["LOOPLAB_MEMORY_DIR"]); md.mkdir(parents=True, exist_ok=True)
    (md / "lessons.jsonl").write_bytes(b"\n".join(orjson.dumps(x) for x in [
        {"statement": "solid", "outcome": "supported", "evidence": [1], "run_id": "r1", "task_id": "t"},
        {"statement": "mixed one", "outcome": "supported", "evidence": [1], "run_id": "rA", "task_id": "t"},
        {"statement": "mixed one", "outcome": "tested", "evidence": [2], "run_id": "rB", "task_id": "t"},
    ]) + b"\n")
    client = TestClient(make_app(tmp_path))
    out = client.get("/api/cross-run/claims", params={"contested": True}).json()["claims"]
    stmts = {c["statement"] for c in out}
    assert "mixed one" in stmts and "solid" not in stmts
