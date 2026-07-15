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


def test_empty_statement_decision_is_400(tmp_path):
    # the other record_claim_decision guard (ValueError 'empty statement') must surface as a clean 400,
    # not a 500 — the operator write validates both the decision AND the statement.
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/cross-run/claim-decide", json={"statement": "   ", "decision": "ratified"})
    assert r.status_code == 400


def test_concept_merge_and_split_routes(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    # merge (CR1a): landed as an append-only alias
    m = client.post("/api/cross-run/concept-merge", json={"from_concept": "hn", "to_concept": "hard-neg"})
    assert m.status_code == 200 and m.json()["alias"]["to"] == "hard-neg"
    # a cycle-closing merge is rejected at the write
    client.post("/api/cross-run/concept-merge", json={"from_concept": "hard-neg", "to_concept": "z"})
    bad = client.post("/api/cross-run/concept-merge", json={"from_concept": "z", "to_concept": "hn"})
    assert bad.status_code == 400
    # split (§21.20.13): a re-tag rule is recorded
    s = client.post("/api/cross-run/concept-split", json={
        "from_concept": "data/aug",
        "rules": [{"to": "data/hard-neg", "when_any": ["hard"]}], "default": "data/aug"})
    assert s.status_code == 200 and s.json()["split"]["rules"][0]["to"] == "data/hard-neg"


def test_concept_steward_endpoints(tmp_path, monkeypatch):
    from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule
    md = Path(os.environ["LOOPLAB_MEMORY_DIR"]); md.mkdir(parents=True, exist_ok=True)
    s = ConceptCapsuleStore(md / "concept_capsules.jsonl")
    s.add(build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max",
                                concepts=["data/hn"], concept_outcomes={}))
    s.add(build_concept_capsule(run_id="r2", fingerprint=["k"], direction="max",
                                concepts=["data/hard-negative-mining"], concept_outcomes={}))

    class _C:
        def complete_tool(self, m, j):
            return {"merges": [{"from_concept": "data/hn", "to_concept": "data/hard-negative-mining"}],
                    "splits": [], "purges": []}

        def complete_text(self, m):
            return "{}"

    import looplab.core.llm as _llm
    monkeypatch.setattr(_llm, "make_llm_client", lambda *a, **k: _C())
    client = TestClient(make_app(tmp_path))
    # operator-triggered agentic run, applying the proposal
    r = client.post("/api/cross-run/concept-steward", params={"apply": True})
    assert r.status_code == 200 and r.json()["proposals"]["merges"][0]["to_concept"] == "data/hard-negative-mining"
    # the merge landed through the reversible governance write
    from looplab.engine.concept_registry import load_concept_aliases
    assert load_concept_aliases(str(md))["data/hn"] == "data/hard-negative-mining"


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
