"""PART V §22 — the cross-run HTTP surface: Research Atlas data (read) + operator claim decisions (write).

Through FastAPI's TestClient: GET atlas/claims read the portfolio; POST claim-decide is the operator
governance write, honored on the next read (rejected → maturity operator-rejected). Agents never use this
router — it is the human/UI surface (§22.4).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import orjson
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient as _FastApiTestClient  # noqa: E402

from looplab.serve.routers.cross_run import _portfolio_identity  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


_GOVERNED_BODY_PATHS = frozenset({
    "/api/cross-run/claim-decide",
    "/api/cross-run/concept-merge",
    "/api/cross-run/concept-purge",
    "/api/cross-run/concept-alias-clear",
    "/api/cross-run/concept-split",
    "/api/cross-run/concept-split-clear",
})
_PAID_STEWARD_PATHS = frozenset({
    "/api/cross-run/concept-steward",
    "/api/cross-run/claim-steward",
})


class TestClient(_FastApiTestClient):
    """Owner-client fixture: propagate the portfolio identity read by a current UI/client."""

    @staticmethod
    def _portfolio_id() -> str:
        return _portfolio_identity(Path(os.environ["LOOPLAB_MEMORY_DIR"]))[1]

    def post(self, url, *args, **kwargs):
        path = str(url).split("?", 1)[0]
        if path in _GOVERNED_BODY_PATHS:
            body = dict(kwargs.get("json") or {})
            body.setdefault("expected_portfolio_id", self._portfolio_id())
            kwargs["json"] = body
        elif path in _PAID_STEWARD_PATHS:
            params = dict(kwargs.get("params") or {})
            params.setdefault("expected_portfolio_id", self._portfolio_id())
            kwargs["params"] = params
        return super().post(url, *args, **kwargs)


def _seed_memory(statement="hard-neg helps"):
    md = Path(os.environ["LOOPLAB_MEMORY_DIR"])   # conftest points this at a tmp dir
    md.mkdir(parents=True, exist_ok=True)
    (md / "lessons.jsonl").write_bytes(orjson.dumps(
        {"statement": statement, "outcome": "supported", "evidence": [1],
         "run_id": "r1", "task_id": "t"}) + b"\n")
    # Owner concept mutations are fenced to the live canonical portfolio projection. Keep a compact
    # vocabulary here so governance tests exercise CAS/action semantics rather than fabricated names.
    (md / "concept_capsules.jsonl").write_bytes(orjson.dumps({
        "v": 2, "concept_evidence": "classifier", "run_id": "concept-seed", "task_id": "t",
        "fingerprint": ["t"],
        "direction": "max", "concepts": [
            "hn", "hard-neg", "z", "data/aug", "data/hard-neg",
            "x", "y", "coarse", "fine", "a", "b",
        ], "concept_outcomes": {},
    }) + b"\n")
    return md


def test_part_iv_v_openapi_publishes_response_envelopes_and_governance_inputs(tmp_path):
    schema = make_app(tmp_path).openapi()
    paths = schema["paths"]

    # CODEX AGENT: an empty `{}` response schema makes generated owner clients fail open to any
    # shape. Keep the top-level evidence/revision envelopes executable even while nested rows evolve.
    expected_responses = {
        ("/api/cross-run/atlas", "get"): "CrossRunAtlasResponse",
        ("/api/cross-run/claims", "get"): "CrossRunClaimsResponse",
        ("/api/cross-run/claim-decide", "post"): "ClaimDecisionResponse",
        ("/api/cross-run/concept-merge", "post"): "ConceptAliasResponse",
        ("/api/cross-run/concept-split", "post"): "ConceptSplitResponse",
        ("/api/cross-run/curation-log", "get"): "CurationLogResponse",
        ("/api/cross-run/concept-steward", "post"): "StewardProposalResponse",
    }
    for (path, method), model in expected_responses.items():
        response = paths[path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert response == {"$ref": f"#/components/schemas/{model}"}

    components = schema["components"]["schemas"]
    assert set(components["CrossRunClaimsResponse"]["required"]) >= {
        "portfolio_id", "claims", "n", "returned", "offset", "limit", "claim_source", "revision",
    }
    # Completeness/source receipts and visible evidence rows are decision authority, not arbitrary JSON.
    # Generated clients must see their versioned fields and enums instead of duplicating server equations
    # over ``Any``.
    claims_schema = components["CrossRunClaimsResponse"]["properties"]
    assert claims_schema["claims"]["items"]["$ref"].endswith("/CrossRunClaim")
    assert claims_schema["claim_source"]["$ref"].endswith("/CrossRunClaimSource")
    claim_schema = components["CrossRunClaim"]
    assert {"epistemic", "maturity", "claim_uid", "evidence_digest",
            "decision_fresh", "research_source", "claim_source"} <= set(
                claim_schema["required"])
    atlas_schema = components["CrossRunAtlasResponse"]["properties"]
    assert atlas_schema["concept_source"]["$ref"].endswith("/CrossRunConceptSource")
    assert atlas_schema["context_pack"]["$ref"].endswith("/CrossRunContextPack")
    assert atlas_schema["governance"]["$ref"].endswith("/CrossRunGovernance")
    decision_ref = paths["/api/cross-run/claim-decide"]["post"]["requestBody"]["content"][
        "application/json"]["schema"]["$ref"]
    decision_schema = components[decision_ref.rsplit("/", 1)[-1]]
    assert set(decision_schema["required"]) >= {
        "expected_portfolio_id", "claim_uid", "evidence_digest", "expected_revision",
        "action_id", "decision",
    }
    steward_parameters = paths["/api/cross-run/concept-steward"]["post"]["parameters"]
    assert any(item["name"] == "action_id" and item["required"] is True
               for item in steward_parameters)
    assert any(item["name"] == "expected_portfolio_id" and item["required"] is True
               for item in steward_parameters)


def test_claims_and_atlas_read(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    r = client.get("/api/cross-run/claims")
    assert r.status_code == 200
    claim = next(c for c in r.json()["claims"] if "hard-neg helps" in c["statement"])
    assert claim["claim_uid"].startswith("clm_") and claim["scope"] == "t"
    assert r.json()["limit"] == 80 and r.json()["revision"] == 0
    assert r.json()["portfolio_id"].startswith("portfolio-sha256:")
    assert len(r.json()["claim_source"]["snapshot_digest"]) == 64
    a = client.get("/api/cross-run/atlas")
    assert a.status_code == 200 and a.json()["n_claims"] >= 1
    assert a.json()["projection"] == "live" and a.json()["page"]["limit"] == 24
    assert a.json()["portfolio_id"] == r.json()["portfolio_id"]
    assert a.json()["claim_source"]["snapshot_digest"] == r.json()["claim_source"]["snapshot_digest"]


def test_delayed_governance_write_cannot_cross_portfolio_reconfiguration(
        tmp_path, monkeypatch):
    first = _seed_memory()
    app = make_app(tmp_path)
    raw_client = _FastApiTestClient(app)
    observed = raw_client.get("/api/cross-run/atlas").json()
    first_id = observed["portfolio_id"]

    replacement = tmp_path / "replacement-memory"
    replacement.mkdir()
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(replacement))
    replacement_id = _portfolio_identity(replacement)[1]
    assert replacement_id != first_id and first != replacement

    response = raw_client.post("/api/cross-run/concept-merge", json={
        "expected_portfolio_id": first_id,
        "from_concept": "hn", "to_concept": "hard-neg",
        "expected_revision": 0, "expected_governance_revision": 0,
        "action_id": "formed-against-first-portfolio",
    })

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == {
        "code": "portfolio_identity_conflict",
        "current_portfolio_id": replacement_id,
    }
    assert not (replacement / "concept_aliases.jsonl").exists()


@pytest.mark.parametrize("endpoint", ["/api/cross-run/claims", "/api/cross-run/atlas"])
def test_claim_snapshot_lock_unavailable_is_stable_503_not_false_empty(
        tmp_path, monkeypatch, endpoint):
    from contextlib import contextmanager

    from looplab.events.eventstore import EventStoreLockError

    _seed_memory()

    @contextmanager
    def _unavailable(path, *, required=False):
        assert required is True
        raise EventStoreLockError(path, OSError("locking unsupported"))
        yield  # pragma: no cover - preserves the contextmanager shape

    monkeypatch.setattr("looplab.events.eventstore._interprocess_lock", _unavailable)
    response = TestClient(make_app(tmp_path)).get(endpoint)

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "cross_run_evidence_unavailable",
        "message": "cross-run evidence cannot be read as one coherent snapshot",
    }


@pytest.mark.parametrize("endpoint", ["/api/cross-run/claims", "/api/cross-run/atlas"])
def test_claim_source_io_unavailable_is_redacted_stable_503(
        tmp_path, monkeypatch, endpoint):
    import looplab.engine.claims as claims_module

    _seed_memory()
    secret_path = "C:/secret/operator/portfolio/lessons.jsonl"
    monkeypatch.setattr(
        claims_module, "load_claim_lessons",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(secret_path)),
    )

    response = TestClient(make_app(tmp_path)).get(endpoint)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == {
        "code": "cross_run_evidence_unavailable",
        "message": "cross-run evidence cannot be read as one coherent snapshot",
    }
    assert secret_path not in response.text


@pytest.mark.parametrize(("endpoint", "target"), [
    ("/api/cross-run/claims", "looplab.engine.claims.claims_for_memory"),
    ("/api/cross-run/atlas", "looplab.engine.claims.atlas_for_memory"),
])
def test_evidence_storage_oserror_is_stable_no_store_503(
        tmp_path, monkeypatch, endpoint, target):
    memory = _seed_memory()
    secret = f"permission denied: {memory / 'private-source.jsonl'}"

    def denied(*_args, **_kwargs):
        raise PermissionError(secret)

    monkeypatch.setattr(target, denied)
    response = TestClient(make_app(tmp_path)).get(endpoint)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == {
        "code": "cross_run_evidence_unavailable",
        "message": "cross-run evidence cannot be read as one coherent snapshot",
    }
    assert secret not in response.text and str(memory) not in response.text


@pytest.mark.parametrize("endpoint", ["/api/cross-run/claims", "/api/cross-run/atlas"])
def test_invalid_utf8_source_row_is_partial_200_and_does_not_hide_valid_tail(
        tmp_path, endpoint):
    md = _seed_memory("valid tail claim")
    path = md / "lessons.jsonl"
    path.write_bytes(b"\xff\n" + path.read_bytes())

    response = TestClient(make_app(tmp_path)).get(endpoint)

    assert response.status_code == 200
    payload = response.json()
    assert payload["claim_source"]["source_complete"] is False
    assert payload["claim_source"]["lessons"] == {
        "read_complete": False,
        "rows_total": 2,
        "rows_retained": 1,
        "rows_quarantined": 1,
        "malformed_rows": 1,
        "invalid_rows": 0,
    }
    if endpoint.endswith("/claims"):
        assert any(row["statement"] == "valid tail claim" for row in payload["claims"])
    else:
        assert payload["n_claims"] == 1


def test_governance_corruption_is_a_no_store_503_without_raw_content(tmp_path):
    memory = _seed_memory()
    poison = "SECRET_GOVERNANCE_ROW_MUST_NOT_LEAK"
    (memory / "claim_decisions.jsonl").write_text(poison + "\n", encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    for endpoint in ("/api/cross-run/claims", "/api/cross-run/atlas"):
        response = client.get(endpoint)
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["detail"] == {
            "v": 1,
            "status": "unavailable", "complete": False,
            "code": "governance_ledger_unavailable",
            "ledger": "claim_decisions", "reason": "malformed_json",
        }
        rendered = response.text
        assert poison not in rendered and str(memory) not in rendered


def test_operator_http_write_cannot_append_over_unhealthy_concept_ledger(tmp_path):
    memory = _seed_memory()
    path = memory / "concept_aliases.jsonl"
    path.write_bytes(b'{"action":"purge","from":"hn"')
    before = path.read_bytes()
    client = TestClient(make_app(tmp_path))

    response = client.post("/api/cross-run/concept-merge", json={
        "from_concept": "hn", "to_concept": "hard-neg",
        "expected_revision": 0, "expected_governance_revision": 0,
        "action_id": "must-not-append",
    })

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "torn_tail"
    assert response.headers["cache-control"] == "no-store"
    assert path.read_bytes() == before


@pytest.mark.parametrize(("kind", "sync_name", "ledger"), [
    ("concept", "strict_fsync", "concept_aliases"),
    ("claim", "strict_fsync_parent", "claim_decisions"),
])
def test_policy_http_never_acknowledges_unconfirmed_durability(
        tmp_path, monkeypatch, kind, sync_name, ledger):
    import looplab.core.atomicio as atomicio_module

    memory = _seed_memory()
    client = TestClient(make_app(tmp_path), raise_server_exceptions=False)
    if kind == "concept":
        endpoint = "/api/cross-run/concept-merge"
        path = memory / "concept_aliases.jsonl"
        body = {
            "from_concept": "hn", "to_concept": "hard-neg",
            "expected_revision": 0, "expected_governance_revision": 0,
            "action_id": "durability-concept",
        }
    else:
        endpoint = "/api/cross-run/claim-decide"
        path = memory / "claim_decisions.jsonl"
        body = _claim_action(client, action_id="durability-claim")

    monkeypatch.setattr(
        atomicio_module, sync_name,
        lambda _value: (_ for _ in ()).throw(OSError("private storage detail")),
    )
    response = client.post(endpoint, json=body)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == {
        "v": 1, "status": "unavailable", "complete": False,
        "code": "governance_ledger_unavailable",
        "ledger": ledger, "reason": "storage_unreadable",
    }
    assert "private storage detail" not in response.text
    before_retry = path.read_bytes()

    # The first attempt may have reached the page cache before sync failed. Same-action retry
    # re-syncs that exact receipt and fails closed again; it never appends a second revision.
    retry = client.post(endpoint, json=body)
    assert retry.status_code == 503
    assert path.read_bytes() == before_retry


def _claim_action(client, *, decision="rejected", action_id="claim-action-1", note=""):
    snapshot = client.get("/api/cross-run/claims").json()
    claim = snapshot["claims"][0]
    return {
        "statement": claim["statement"], "claim_uid": claim["claim_uid"],
        "evidence_digest": claim["evidence_digest"],
        "scope": claim["scope"], "metric": claim.get("metric", ""),
        "decision": decision, "note": note,
        "expected_revision": snapshot["revision"], "action_id": action_id,
    }


def test_operator_decision_write_is_honored(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    body = _claim_action(client, note="overruled")
    r = client.post("/api/cross-run/claim-decide", json=body)
    assert r.status_code == 200 and r.json()["ok"]
    # Lost-response replay is idempotent even though its observed revision is now stale.
    assert client.post("/api/cross-run/claim-decide", json=body).json()["revision"] == r.json()["revision"]
    # the next read reflects the operator verdict
    claims = client.get("/api/cross-run/claims").json()["claims"]
    c = [x for x in claims if "hard-neg helps" in x["statement"]][0]
    assert c["maturity"] == "operator-rejected" and c["decision_fresh"] is True


def test_claim_governance_rejects_stale_revision_and_wrong_uid(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    first = _claim_action(client, action_id="claim-first")
    assert client.post("/api/cross-run/claim-decide", json=first).status_code == 200

    stale = {**first, "action_id": "claim-stale", "decision": "pinned"}
    response = client.post("/api/cross-run/claim-decide", json=stale)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "claim_revision_conflict"

    current = _claim_action(client, action_id="claim-wrong-target")
    current["claim_uid"] = "clm_0000000000000000"
    response = client.post("/api/cross-run/claim-decide", json=current)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "claim_target_changed"


def test_claim_decision_fences_nonexistent_and_changed_evidence(tmp_path):
    from looplab.engine.claim_key import claim_uid

    md = _seed_memory()
    client = TestClient(make_app(tmp_path))
    snapshot = client.get("/api/cross-run/claims").json()
    current = snapshot["claims"][0]

    fabricated = {
        "statement": "future fabricated claim improves score",
        "claim_uid": claim_uid("future fabricated claim improves score", scope="t"),
        "evidence_digest": current["evidence_digest"],
        "scope": "t", "metric": "", "decision": "ratified", "note": "",
        "expected_revision": snapshot["revision"], "action_id": "fabricated-target",
    }
    response = client.post("/api/cross-run/claim-decide", json=fabricated)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "claim_target_missing"

    observed = _claim_action(client, decision="pinned", action_id="stale-evidence")
    with (md / "lessons.jsonl").open("ab") as stream:
        stream.write(orjson.dumps({
            "statement": observed["statement"], "outcome": "supported", "evidence": [2],
            "run_id": "r2", "task_id": observed["scope"],
        }) + b"\n")
    response = client.post("/api/cross-run/claim-decide", json=observed)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "claim_evidence_changed"


def test_scoped_claim_decision_ignores_other_task_rewrite_but_fences_same_task(tmp_path):
    md = Path(os.environ["LOOPLAB_MEMORY_DIR"])
    md.mkdir(parents=True, exist_ok=True)

    def _row(statement, run_id, task_id, node_id):
        return {
            "statement": statement, "outcome": "supported", "evidence": [node_id],
            "run_id": run_id, "task_id": task_id, "direction": "max",
        }

    path = md / "lessons.jsonl"
    path.write_bytes(b"\n".join(orjson.dumps(row) for row in (
        _row("task A claim", "a1", "task-a", 1),
        _row("task B claim", "b1", "task-b", 1),
    )) + b"\n")
    client = TestClient(make_app(tmp_path))
    scoped = client.get("/api/cross-run/claims", params={"scope_task": "task-a"}).json()
    claim = scoped["claims"][0]
    body = {
        "statement": claim["statement"], "claim_uid": claim["claim_uid"],
        "evidence_digest": claim["evidence_digest"], "scope": "task-a", "metric": "",
        "decision": "ratified", "note": "", "expected_revision": scoped["revision"],
        "action_id": "scoped-unrelated-safe",
    }
    with path.open("ab") as stream:
        stream.write(orjson.dumps(_row("task B other", "b2", "task-b", 2)) + b"\n")
    assert client.post("/api/cross-run/claim-decide", json=body).status_code == 200

    fresh = client.get("/api/cross-run/claims", params={"scope_task": "task-a"}).json()
    current = fresh["claims"][0]
    stale = {
        **body, "evidence_digest": current["evidence_digest"],
        "decision": "pinned", "expected_revision": fresh["revision"],
        "action_id": "scoped-same-task-stale",
    }
    with path.open("ab") as stream:
        stream.write(orjson.dumps(_row("task A claim", "a2", "task-a", 3)) + b"\n")
    response = client.post("/api/cross-run/claim-decide", json=stale)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "claim_evidence_changed"


def test_claim_decision_clear_can_remove_policy_after_evidence_is_retired(tmp_path):
    md = _seed_memory()
    client = TestClient(make_app(tmp_path))
    body = _claim_action(client, decision="rejected", action_id="retired-set")
    assert client.post("/api/cross-run/claim-decide", json=body).status_code == 200
    (md / "lessons.jsonl").unlink()
    clear = {**body, "decision": "clear", "action_id": "retired-clear", "expected_revision": 1}
    response = client.post("/api/cross-run/claim-decide", json=clear)
    assert response.status_code == 200 and response.json()["revision"] == 2


def test_scoped_row_cannot_silently_clear_inherited_global_decision(tmp_path):
    from looplab.engine.claims import record_claim_decision

    md = _seed_memory()
    record_claim_decision(md, statement="hard-neg helps", decision="rejected",
                          expected_revision=0, action_id="global-set")
    client = TestClient(make_app(tmp_path))
    snapshot = client.get("/api/cross-run/claims").json()
    row = snapshot["claims"][0]
    assert row["scope"] == "t" and row["maturity"] == "operator-rejected"
    assert row["decision"]["scope"] == "" and row["decision"]["claim_uid"] != row["claim_uid"]

    ambiguous = {
        "statement": row["statement"], "claim_uid": row["claim_uid"],
        "evidence_digest": row["evidence_digest"], "scope": row["scope"],
        "metric": row.get("metric", ""), "decision": "clear", "note": "",
        "expected_revision": snapshot["revision"], "action_id": "ambiguous-clear",
    }
    response = client.post("/api/cross-run/claim-decide", json=ambiguous)
    assert response.status_code == 409
    target = response.json()["detail"]
    assert target["code"] == "claim_clear_target_mismatch"
    assert target["claim_uid"] == row["decision"]["claim_uid"] and target["scope"] == ""

    explicit = {**ambiguous, "claim_uid": target["claim_uid"], "scope": target["scope"],
                "metric": target["metric"], "action_id": "explicit-global-clear"}
    cleared = client.post("/api/cross-run/claim-decide", json=explicit)
    assert cleared.status_code == 200 and cleared.json()["revision"] == 2
    fresh = client.get("/api/cross-run/claims").json()["claims"][0]
    assert fresh["maturity"] == "machine-proposed" and fresh["decision"] is None


def test_claims_pagination_is_bounded_and_reports_total(tmp_path):
    md = Path(os.environ["LOOPLAB_MEMORY_DIR"])
    md.mkdir(parents=True, exist_ok=True)
    (md / "lessons.jsonl").write_bytes(b"\n".join(orjson.dumps({
        "statement": f"claim {index} helps", "outcome": "supported", "evidence": [index],
        "run_id": f"r{index}", "task_id": "t",
    }) for index in range(5)) + b"\n")
    client = TestClient(make_app(tmp_path))
    payload = client.get("/api/cross-run/claims", params={"limit": 2, "offset": 2}).json()
    assert payload["n"] == 5 and payload["returned"] == 2
    assert payload["limit"] == 2 and payload["offset"] == 2
    assert client.get("/api/cross-run/claims", params={"limit": 201}).status_code == 422


def test_cross_run_reads_apply_independent_nested_caps(tmp_path):
    md = Path(os.environ["LOOPLAB_MEMORY_DIR"])
    md.mkdir(parents=True, exist_ok=True)
    (md / "lessons.jsonl").write_bytes(b"\n".join(orjson.dumps({
        "statement": "bounded evidence claim", "outcome": "supported", "evidence": [index],
        "run_id": f"r{index:03}", "task_id": "t",
    }) for index in range(80)) + b"\n")
    (md / "concept_capsules.jsonl").write_bytes(b"\n".join(orjson.dumps({
        "v": 2, "concept_evidence": "classifier", "run_id": f"r{index:03}",
        "task_id": "t", "fingerprint": ["t"],
        "direction": "max", "concepts": ["shared"], "concept_outcomes": {"shared": index},
    }) for index in range(70)) + b"\n")
    client = TestClient(make_app(tmp_path))

    claim = client.get("/api/cross-run/claims", params={"limit": 1}).json()["claims"][0]
    explored = client.get("/api/cross-run/atlas", params={"limit": 1}).json()["explored"][0]

    assert claim["n_support"] == 80 and len(claim["support"]) == len(claim["runs"]) == 64
    assert claim["nested_omitted"] == {"support": 16, "runs": 16}
    assert explored["n_runs"] == 70 and len(explored["runs"]) == 64
    assert explored["runs_omitted"] == 6


def test_invalid_decision_is_400(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    body = _claim_action(client)
    body["decision"] = "bogus"
    r = client.post("/api/cross-run/claim-decide", json=body)
    assert r.status_code == 422


def test_empty_statement_decision_is_400(tmp_path):
    # the other record_claim_decision guard (ValueError 'empty statement') must surface as a clean 400,
    # not a 500 — the operator write validates both the decision AND the statement.
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    body = _claim_action(client, decision="ratified")
    body["statement"] = "   "
    r = client.post("/api/cross-run/claim-decide", json=body)
    assert r.status_code == 422


def test_concept_merge_and_split_routes(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    m = client.post("/api/cross-run/concept-merge", json={
        "from_concept": "hn", "to_concept": "hard-neg",
        "expected_revision": 0, "expected_governance_revision": 0,
        "action_id": "alias-merge-1"})
    assert (m.status_code == 200 and m.json()["alias"]["to"] == "hard-neg"
            and m.json()["governance_revision"] == 1)
    second = client.post("/api/cross-run/concept-merge", json={
        "from_concept": "hard-neg", "to_concept": "z",
        "expected_revision": 1, "expected_governance_revision": 1,
        "action_id": "alias-merge-2"})
    assert second.status_code == 200 and second.json()["governance_revision"] == 2
    bad = client.post("/api/cross-run/concept-merge", json={
        "from_concept": "z", "to_concept": "hn",
        "expected_revision": 2, "expected_governance_revision": 2,
        "action_id": "alias-merge-3"})
    assert bad.status_code == 422
    s = client.post("/api/cross-run/concept-split", json={
        "from_concept": "data/aug",
        "rules": [{"to": "data/hard-neg", "when_any": ["hard"]}],
        "expected_revision": 0, "expected_governance_revision": 2,
        "action_id": "split-set-1"})
    assert (s.status_code == 200 and s.json()["split"]["rules"][0]["to"] == "data/hard-neg"
            and s.json()["governance_revision"] == 3)


def test_concept_http_fences_nonexistent_entities_but_split_can_create_children(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))

    missing_source = client.post("/api/cross-run/concept-merge", json={
        "from_concept": "typo/source", "to_concept": "x", "expected_revision": 0,
        "expected_governance_revision": 0, "action_id": "missing-source",
    })
    missing_target = client.post("/api/cross-run/concept-merge", json={
        "from_concept": "x", "to_concept": "typo/target", "expected_revision": 0,
        "expected_governance_revision": 0, "action_id": "missing-target",
    })
    assert missing_source.status_code == missing_target.status_code == 422
    assert "does not exist" in missing_source.text and "does not exist" in missing_target.text
    assert client.get("/api/cross-run/atlas").json()["revisions"]["concept_governance"] == 0

    split = client.post("/api/cross-run/concept-split", json={
        "from_concept": "coarse",
        "rules": [{"to": "new/provisional-child", "when_any": ["match"]}],
        "expected_revision": 0, "expected_governance_revision": 0,
        "action_id": "split-creates-child",
    })
    assert split.status_code == 200
    receipt = split.json()["split"]
    assert len(receipt["concept_snapshot_digest"]) == 64 and receipt["concept_snapshot_count"] > 0

    missing_clear = client.post("/api/cross-run/concept-alias-clear", json={
        "from_concept": "x", "expected_revision": 0,
        "expected_governance_revision": 1, "action_id": "clear-without-policy",
    })
    assert missing_clear.status_code == 422 and "no active alias" in missing_clear.text


def test_governance_cas_typed_purge_and_reversible_clear(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    merge = {"from_concept": "hn", "to_concept": "hard-neg",
             "expected_revision": 0, "expected_governance_revision": 0,
             "action_id": "merge-retry"}
    first = client.post("/api/cross-run/concept-merge", json=merge)
    assert (first.status_code == 200 and first.json()["revision"] == 1
            and first.json()["governance_revision"] == 1)
    retry = client.post("/api/cross-run/concept-merge", json=merge).json()
    assert retry["revision"] == 1 and retry["governance_revision"] == 1

    stale = client.post("/api/cross-run/concept-merge", json={
        "from_concept": "x", "to_concept": "y",
        "expected_revision": 0, "expected_governance_revision": 1,
        "action_id": "stale-merge"})
    assert stale.status_code == 409 and stale.json()["detail"]["current_revision"] == 1
    assert client.post("/api/cross-run/concept-merge", json={
        "from_concept": "x", "to_concept": "", "expected_revision": 1,
        "expected_governance_revision": 1,
        "action_id": "unsafe-empty"}).status_code == 422

    purge = client.post("/api/cross-run/concept-purge", json={
        # `hn` is now an alias; destructive actions must name the live canonical concept.
        "from_concept": "hard-neg", "confirm": "purge", "expected_revision": 1,
        "expected_governance_revision": 1,
        "action_id": "purge-explicit"})
    assert (purge.status_code == 200 and purge.json()["alias"]["action"] == "purge"
            and purge.json()["governance_revision"] == 2)
    clear = client.post("/api/cross-run/concept-alias-clear", json={
        "from_concept": "hn", "expected_revision": 2,
        "expected_governance_revision": 2, "action_id": "alias-clear"})
    assert (clear.status_code == 200 and clear.json()["alias"]["action"] == "clear"
            and clear.json()["governance_revision"] == 3)


@pytest.mark.parametrize("value", [None, True, -1])
def test_concept_http_requires_strict_global_governance_revision(tmp_path, value):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    body = {
        "from_concept": "x", "to_concept": "y", "expected_revision": 0,
        "action_id": "invalid-global-revision",
    }
    if value is not None:
        body["expected_governance_revision"] = value
    response = client.post("/api/cross-run/concept-merge", json=body)
    assert response.status_code == 422


def test_concept_global_cas_fences_alias_and_split_ledgers(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    alias = client.post("/api/cross-run/concept-merge", json={
        "from_concept": "x", "to_concept": "y", "expected_revision": 0,
        "expected_governance_revision": 0, "action_id": "global-alias-first",
    })
    assert alias.status_code == 200 and alias.json()["governance_revision"] == 1

    stale_split = client.post("/api/cross-run/concept-split", json={
        "from_concept": "coarse", "rules": [{"to": "fine", "when_any": ["match"]}],
        "expected_revision": 0, "expected_governance_revision": 0,
        "action_id": "global-split-stale",
    })
    assert stale_split.status_code == 409
    detail = stale_split.json()["detail"]
    assert detail == {
        "code": "concept_governance_revision_conflict",
        "expected_governance_revision": 0,
        "current_governance_revision": 1,
    }

    split = client.post("/api/cross-run/concept-split", json={
        "from_concept": "coarse", "rules": [{"to": "fine", "when_any": ["match"]}],
        "expected_revision": 0, "expected_governance_revision": 1,
        "action_id": "global-split-current",
    })
    assert (split.status_code == 200 and split.json()["revision"] == 1
            and split.json()["governance_revision"] == 2)

    stale_alias = client.post("/api/cross-run/concept-merge", json={
        "from_concept": "a", "to_concept": "b", "expected_revision": 1,
        "expected_governance_revision": 1, "action_id": "global-alias-stale",
    })
    assert stale_alias.status_code == 409
    assert stale_alias.json()["detail"]["current_governance_revision"] == 2
    atlas = client.get("/api/cross-run/atlas").json()
    assert atlas["revisions"]["concept_governance"] == 2
    assert atlas["revisions"]["concept_aliases"] == atlas["revisions"]["concept_splits"] == 1


def test_concept_steward_endpoints(tmp_path, monkeypatch):
    from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule
    md = Path(os.environ["LOOPLAB_MEMORY_DIR"])
    md.mkdir(parents=True, exist_ok=True)
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
    assert client.post("/api/cross-run/concept-steward", params={
        "apply": True, "action_id": "steward-apply-forbidden"}).status_code == 422
    r = client.post("/api/cross-run/concept-steward", params={"action_id": "steward-proposal-1"})
    assert r.status_code == 200 and r.json()["proposals"]["merges"][0]["to_concept"] == "data/hard-negative-mining"
    assert r.json()["receipt"] is None and r.json()["invocation"]["outcome"] == "proposed"
    # The LLM proposal is audited but never mutates meaning; an operator must select a typed action.
    from looplab.engine.concept_registry import load_concept_aliases
    assert load_concept_aliases(str(md)) == {}
    history = client.get("/api/cross-run/curation-log").json()
    assert history["v"] == 1 and history["status"] == "complete" and history["complete"] is True
    log = history["entries"]
    assert log[0]["action_id"] == "steward-proposal-1"
    # A lost-response retry is served from the durable invocation receipt.
    retry = client.post("/api/cross-run/concept-steward", params={"action_id": "steward-proposal-1"})
    assert retry.json()["invocation"]["revision"] == r.json()["invocation"]["revision"]


def test_steward_error_is_redacted_in_response_and_audit_log(tmp_path, monkeypatch):
    import looplab.core.llm as llm_module
    import looplab.engine.concept_steward as steward_module

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    monkeypatch.setattr(llm_module, "make_llm_client", lambda *args, **kwargs: object())

    def fail(*args, **kwargs):
        raise RuntimeError(f"provider failed Authorization: Bearer {secret}")

    monkeypatch.setattr(steward_module, "steward_concepts", fail)
    client = TestClient(make_app(tmp_path))
    response = client.post("/api/cross-run/concept-steward", params={"action_id": "redacted-error"})
    assert response.status_code == 400
    log = (Path(os.environ["LOOPLAB_MEMORY_DIR"]) / "concept_curation_log.jsonl").read_text()
    persisted = response.text + log
    assert secret not in persisted and "steward:" in persisted.lower()


@pytest.mark.parametrize(("kind", "log_endpoint", "steward_endpoint"), [
    ("concept", "/api/cross-run/curation-log", "/api/cross-run/concept-steward"),
    ("claim", "/api/cross-run/claim-curation-log", "/api/cross-run/claim-steward"),
])
def test_poisoned_curation_history_is_503_and_never_replays_paid_steward(
        tmp_path, monkeypatch, kind, log_endpoint, steward_endpoint):
    import looplab.core.llm as llm_module

    memory = Path(os.environ["LOOPLAB_MEMORY_DIR"])
    memory.mkdir(parents=True, exist_ok=True)
    path = memory / f"{kind}_curation_log.jsonl"
    poison = "SECRET_CURRATION_ROW_MUST_NOT_LEAK"
    path.write_text(poison + "\n", encoding="utf-8")
    before = path.read_bytes()
    client_creations: list[int] = []

    def paid_client(*_args, **_kwargs):
        client_creations.append(1)
        return object()

    monkeypatch.setattr(llm_module, "make_llm_client", paid_client)
    client = TestClient(make_app(tmp_path))

    for response in (
        client.get(log_endpoint),
        client.post(steward_endpoint, params={"action_id": "must-not-replay"}),
    ):
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["detail"] == {
            "v": 1, "status": "unavailable", "complete": False,
            "code": "governance_ledger_unavailable",
            "ledger": f"{kind}_curation", "reason": "malformed_json",
        }
        assert poison not in response.text and str(memory) not in response.text

    assert client_creations == []
    assert path.read_bytes() == before


def test_known_finalize_and_legacy_curation_rows_remain_complete_paid_history(
        tmp_path, monkeypatch):
    import looplab.core.llm as llm_module

    memory = Path(os.environ["LOOPLAB_MEMORY_DIR"])
    memory.mkdir(parents=True, exist_ok=True)
    path = memory / "claim_curation_log.jsonl"
    digest = "a" * 64
    source = json.dumps(
        {"v": 1, "run_id": "r1", "task_id": "t", "finish_seq": 7},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    rows = [{
        "v": 2, "curation_key": f"claim:v2:{digest}",
        "source_key": "source:v1:" + hashlib.sha256(source).hexdigest(),
        "run_id": "r1", "task_id": "t", "finish_seq": 7,
        "input_digest": digest, "input_schema": "finalize-claim-curation/v1",
        "model": "m", "parser": "tool_call_once", "outcome": "empty",
        "auto": False, "auto_requested": False,
        "proposals": {"decisions": []}, "receipt": None, "revision": 1,
    }, {
        # Known oldest HTTP audit shape: no discriminator, but its action id remains a
        # terminal at-most-once receipt and can never become a new paid cache miss.
        "action_id": "legacy-paid", "revision": 2,
        "proposals": {"decisions": []}, "receipt": None,
    }]
    path.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in rows))
    client_creations: list[int] = []
    monkeypatch.setattr(
        llm_module, "make_llm_client",
        lambda *_args, **_kwargs: client_creations.append(1) or object(),
    )
    client = TestClient(make_app(tmp_path))

    history = client.get("/api/cross-run/claim-curation-log")
    assert history.status_code == 200
    assert history.json()["complete"] is True and history.json()["n"] == 2
    retry = client.post(
        "/api/cross-run/claim-steward", params={"action_id": "legacy-paid"})
    assert retry.status_code == 200 and retry.json()["invocation"]["action_id"] == "legacy-paid"
    assert client_creations == []


def test_concurrent_steward_retry_pays_for_one_llm_invocation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    import looplab.core.llm as llm_module
    import looplab.engine.concept_steward as steward_module

    entered, release, counter_lock = Event(), Event(), Lock()
    calls: list[int] = []

    def fake_steward(*args, **kwargs):
        with counter_lock:
            calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None}

    monkeypatch.setattr(llm_module, "make_llm_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(steward_module, "steward_concepts", fake_steward)
    app = make_app(tmp_path)
    clients = (TestClient(app), TestClient(app))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(clients[0].post, "/api/cross-run/concept-steward",
                                params={"action_id": "same-action"})
        assert entered.wait(5)
        second = executor.submit(clients[1].post, "/api/cross-run/concept-steward",
                                 params={"action_id": "same-action"})
        release.set()
        responses = (first.result(timeout=10), second.result(timeout=10))
    assert len(calls) == 1
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json()["invocation"]["revision"] == responses[1].json()["invocation"]["revision"]


def test_steward_paid_call_without_terminal_receipt_is_not_replayed(tmp_path, monkeypatch):
    import looplab.core.llm as llm_module
    import looplab.engine.concept_registry as registry_module
    import looplab.engine.concept_steward as steward_module

    calls: list[int] = []

    def fake_steward(*_args, **_kwargs):
        calls.append(1)
        return {"proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None}

    monkeypatch.setattr(llm_module, "make_llm_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(steward_module, "steward_concepts", fake_steward)
    real_append = registry_module._append_governance

    def lose_terminal_receipt(path, record, **kwargs):
        if record.get("action") == "steward-invocation":
            raise RuntimeError("simulated process loss after the provider returned")
        return real_append(path, record, **kwargs)

    monkeypatch.setattr(registry_module, "_append_governance", lose_terminal_receipt)
    app = make_app(tmp_path)
    first = TestClient(app, raise_server_exceptions=False).post(
        "/api/cross-run/concept-steward", params={"action_id": "paid-ambiguous"},
    )
    assert first.status_code == 500 and calls == [1]

    monkeypatch.setattr(registry_module, "_append_governance", real_append)
    retry = TestClient(app).post(
        "/api/cross-run/concept-steward", params={"action_id": "paid-ambiguous"},
    )
    assert retry.status_code == 409
    detail = retry.json()["detail"]
    assert detail["code"] == "steward_invocation_outcome_unknown"
    assert detail["invocation"]["action_id"] == "paid-ambiguous"
    assert calls == [1]


def test_steward_does_not_start_paid_call_without_durable_begin_claim(tmp_path, monkeypatch):
    import looplab.core.atomicio as atomicio_module
    import looplab.core.llm as llm_module
    import looplab.engine.concept_steward as steward_module

    calls: list[int] = []

    def fake_steward(*_args, **_kwargs):
        calls.append(1)
        return {"proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None}

    monkeypatch.setattr(llm_module, "make_llm_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(steward_module, "steward_concepts", fake_steward)
    monkeypatch.setattr(atomicio_module, "strict_fsync",
                        lambda _fileno: (_ for _ in ()).throw(OSError("sync unavailable")))

    response = TestClient(make_app(tmp_path), raise_server_exceptions=False).post(
        "/api/cross-run/concept-steward", params={"action_id": "no-durable-claim"},
    )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == {
        "v": 1, "status": "unavailable", "complete": False,
        "code": "governance_ledger_unavailable",
        "ledger": "concept_curation", "reason": "storage_unreadable",
    }
    assert calls == []


def test_contested_filter(tmp_path):
    md = Path(os.environ["LOOPLAB_MEMORY_DIR"])
    md.mkdir(parents=True, exist_ok=True)
    (md / "lessons.jsonl").write_bytes(b"\n".join(orjson.dumps(x) for x in [
        {"statement": "solid", "outcome": "supported", "evidence": [1], "run_id": "r1", "task_id": "t"},
        {"statement": "mixed one", "outcome": "supported", "evidence": [1], "run_id": "rA", "task_id": "t"},
        {"statement": "mixed one", "outcome": "tested", "evidence": [2], "run_id": "rB", "task_id": "t"},
    ]) + b"\n")
    client = TestClient(make_app(tmp_path))
    out = client.get("/api/cross-run/claims", params={"contested": True}).json()["claims"]
    stmts = {c["statement"] for c in out}
    assert "mixed one" in stmts and "solid" not in stmts
