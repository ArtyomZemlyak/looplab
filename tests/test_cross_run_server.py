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
    # Owner concept mutations are fenced to the live canonical portfolio projection. Keep a compact
    # vocabulary here so governance tests exercise CAS/action semantics rather than fabricated names.
    (md / "concept_capsules.jsonl").write_bytes(orjson.dumps({
        "v": 1, "run_id": "concept-seed", "task_id": "t", "fingerprint": ["t"],
        "direction": "max", "concepts": [
            "hn", "hard-neg", "z", "data/aug", "data/hard-neg",
            "x", "y", "coarse", "fine", "a", "b",
        ], "concept_outcomes": {},
    }) + b"\n")
    return md


def test_claims_and_atlas_read(tmp_path):
    _seed_memory()
    client = TestClient(make_app(tmp_path))
    r = client.get("/api/cross-run/claims")
    assert r.status_code == 200
    claim = next(c for c in r.json()["claims"] if "hard-neg helps" in c["statement"])
    assert claim["claim_uid"].startswith("clm_") and claim["scope"] == "t"
    assert r.json()["limit"] == 80 and r.json()["revision"] == 0
    a = client.get("/api/cross-run/atlas")
    assert a.status_code == 200 and a.json()["n_claims"] >= 1
    assert a.json()["projection"] == "live" and a.json()["page"]["limit"] == 24


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
    log = client.get("/api/cross-run/curation-log").json()["entries"]
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

    assert response.status_code == 500
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
