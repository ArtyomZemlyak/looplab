"""Security contract for Part IV/V cross-run projections and prompt reuse."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest

from looplab.core.models import RunState
from looplab.engine.lessons_priors import LessonPriorsMixin
from looplab.engine.proposal_cues import ProposalCuesMixin
from looplab.engine.strategy import StrategyCadenceMixin
from looplab.tools.cross_run_tools import CrossRunTools
from looplab.trust.cross_run import cross_run_text, sanitize_cross_run_projection


_SECRET = "sk-abcdefghijklmnopqrstuvwxyz012345"


def _lesson(statement: str, *, run_id: str = "r1") -> dict:
    return {"statement": statement, "outcome": "supported", "evidence": [1],
            "run_id": run_id, "task_id": "t", "direction": "max"}


def test_recursive_projection_masks_nested_keys_strings_and_malformed_objects():
    class _BadString:
        def __str__(self):
            raise RuntimeError(f"password={_SECRET}")

    opaque_action = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
    projected = sanitize_cross_run_projection({
        "safe": [{"api_key": "short-secret", "detail": f"Authorization: Bearer {_SECRET}"}],
        "malformed": _BadString(),
        "oversized": [str(index) for index in range(300)],
        "action_id": opaque_action,
    }, max_items=32, max_total_items=128)

    rendered = json.dumps(projected)
    assert projected["safe"][0]["api_key"] == "***"
    assert _SECRET not in rendered and "short-secret" not in rendered
    assert projected["malformed"] == "<unavailable>"
    assert len(projected["oversized"]) == 32
    assert projected["action_id"] == opaque_action
    clipped = cross_run_text("line\n" * 100, max_chars=80, single_line=True)
    assert "\n" not in clipped and len(clipped) <= 80


def test_secret_key_masking_uses_the_original_key_not_the_bounded_display_key():
    # Regression: the secret-key gate must classify the ORIGINAL structured key, never the
    # truncated/entropy-masked display key. A secret name that the 160-char cap or the entropy mask
    # rewrites would otherwise fail is_secret_key_name and leak its child value in the clear.
    high_entropy_secret_key = "token_a1b2c3d4e5f6g7h8i9j0k1l2"   # >=24 chars -> entropy-masked display key
    oversized_secret_key = (
        "config.services.external.providers.vault.rotation.primary.region."
        + "x" * 120 + ".openai_api_key")                        # >160 chars -> truncated display key
    projected = sanitize_cross_run_projection({
        high_entropy_secret_key: "hunter2pass!",
        oversized_secret_key: "sk-REALSECRETVALUE1234",
    })
    rendered = json.dumps(projected)
    assert "hunter2pass!" not in rendered and "sk-REALSECRETVALUE1234" not in rendered
    assert set(projected.values()) == {"***"}


@pytest.mark.parametrize("payload", [
    {"decision": "ratified", "ｄｅｃｉｓｉｏｎ": "rejected"},
    {"ｄｅｃｉｓｉｏｎ": "rejected", "decision": "ratified"},
])
def test_nfkc_key_collision_cannot_override_exact_governance_field(payload):
    projected = sanitize_cross_run_projection(payload)

    assert projected == {"decision": "ratified"}


def test_opaque_run_and_task_ids_keep_unicode_identity_through_claim_projection():
    from looplab.engine.claims import claim_assessments

    rows = claim_assessments([
        {**_lesson("dropout improves generalization", run_id="Ａ"), "task_id": "Ａ"},
        {**_lesson("dropout improves generalization", run_id="A"), "task_id": "A"},
    ], structured=True)

    assert len(rows) == 2
    assert {row["scope"] for row in rows} == {"Ａ", "A"}
    assert {tuple(row["runs"]) for row in rows} == {("Ａ",), ("A",)}
    assert {ref for row in rows for ref in row["support"]} == {"Ａ:1", "A:1"}


def test_opaque_identity_projection_still_masks_secrets_and_controls():
    projected = sanitize_cross_run_projection({
        "run_id": "Ａ\x00safe",
        "task_id": "ｐａｓｓｗｏｒｄ＝tiny-secret",
        "invocation_id": "Authorization: Bearer tiny-secret",
    })

    assert projected["run_id"] == "Ａ safe"
    assert projected["task_id"] == "***"
    assert projected["invocation_id"] == "Authorization: Bearer ***"
    assert "tiny-secret" not in json.dumps(projected, ensure_ascii=False)


def test_cross_run_tool_redacts_legacy_memory_and_bounds_every_result(tmp_path):
    statement = f"password={_SECRET} improves retrieval"
    run_id = f"https://user:{_SECRET}@provider.invalid/run"
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(_lesson(statement, run_id=run_id)) + b"\n")

    tools = CrossRunTools(tmp_path)
    result = tools.execute("cross_run_claims", {})
    unknown = tools.execute(f"unknown-{_SECRET}", {"api_key": "short-secret"})

    assert len(result) <= 16_000
    assert "password=***" in result and "***:***@provider.invalid" in result
    assert _SECRET not in result + unknown and "short-secret" not in result + unknown


def test_research_claim_and_governance_writers_never_persist_raw_secrets(tmp_path):
    from looplab.engine.claims import record_claim_decision, record_research_claims

    statement = f"password={_SECRET} improves retrieval"
    record_research_claims(tmp_path, run_id="r1", task_id="t", direction="max", claims=[{
        "statement": statement, "node_ids": [1],
        "urls": [f"https://user:{_SECRET}@provider.invalid/?token={_SECRET}"],
        "verification": {"verdict": "supported", "method": "llm",
                         "note": f"Authorization: Bearer {_SECRET}"},
    }])
    record_claim_decision(
        tmp_path, statement=statement, decision="ratified",
        note=f"api_key={_SECRET}", action_id="security-decision")

    persisted = ((tmp_path / "research_claims.jsonl").read_text(encoding="utf-8")
                 + (tmp_path / "claim_decisions.jsonl").read_text(encoding="utf-8"))
    assert _SECRET not in persisted
    assert "password=***" in persisted and "api_key=***" in persisted


def test_legacy_governance_retry_returns_a_redacted_receipt(tmp_path):
    from looplab.engine.claims import record_claim_decision

    statement = f"password={_SECRET} improves retrieval"
    legacy = {
        "statement": statement, "scope": "t", "metric": "", "decision": "ratified",
        "note": f"api_key={_SECRET}", "action_id": "legacy-security-retry", "revision": 1,
    }
    (tmp_path / "claim_decisions.jsonl").write_text(
        json.dumps(legacy) + "\n", encoding="utf-8")

    replay = record_claim_decision(
        tmp_path, statement=statement, scope="t", decision="ratified",
        note=f"api_key={_SECRET}", action_id="legacy-security-retry")

    assert _SECRET not in json.dumps(replay)
    assert replay["statement"].startswith("password=***") and replay["note"] == "api_key=***"


def test_all_cross_run_prompt_pushes_share_the_redaction_contract(tmp_path):
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(
        _lesson(f"password={_SECRET} improves retrieval")) + b"\n")
    (tmp_path / "meta_notes.jsonl").write_bytes(orjson.dumps({
        "task_id": "t", "note": f"Authorization: Bearer {_SECRET}"}) + b"\n")

    class _ProposalHost(ProposalCuesMixin):
        _cross_run_advisory = True
        _cross_run_structured_claims = True

        def __init__(self):
            self.memory_dir = str(tmp_path)

    class _StrategyHost(StrategyCadenceMixin):
        _cross_run_advisory = True
        _cross_run_structured_claims = True

        def __init__(self):
            self.memory_dir = str(tmp_path)

    class _PriorHost(LessonPriorsMixin):
        def __init__(self):
            self._e = SimpleNamespace(_lesson_abstractor=None, task=SimpleNamespace(id="t", goal=""))

    state = RunState(run_id="current", task_id="t", direction="max")
    proposal_host = _ProposalHost()
    proposal = proposal_host._cross_run_advisory_text(state)
    first_digest = proposal_host._cross_run_advisory_receipt["corpus_digest"]
    replacement = "sk-ZYXWVUTSRQPONMLKJIHGFEDCBA987654"
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(
        _lesson(f"password={replacement} improves retrieval")) + b"\n")
    second_host = _ProposalHost()
    second_host._cross_run_advisory_text(state)
    strategy = _StrategyHost()._cross_run_note_for_ctx(state)
    prior = _PriorHost()._render_role_prior((
        [f"Authorization: Bearer {_SECRET}"],
        [(0, _lesson(f"password={_SECRET} improves retrieval"))],
        [], lambda _text: [],
    ), None)

    combined = proposal + strategy + prior
    assert first_digest == second_host._cross_run_advisory_receipt["corpus_digest"]
    assert _SECRET not in combined and replacement not in combined
    assert "password=***" in combined and "bearer ***" in combined.lower()


def test_http_claim_atlas_and_curation_projections_redact_nested_legacy_data(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app

    memory = Path(os.environ["LOOPLAB_MEMORY_DIR"])
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "lessons.jsonl").write_bytes(orjson.dumps(
        _lesson(f"password={_SECRET} improves retrieval")) + b"\n")
    from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule
    ConceptCapsuleStore(memory / "concept_capsules.jsonl").add(build_concept_capsule(
        run_id="r1", task_id="t", fingerprint=["retrieval"], direction="max",
        concepts=[f"token/{_SECRET}"], concept_outcomes={}))
    (memory / "claim_curation_log.jsonl").write_bytes(orjson.dumps({
        "action_id": "legacy-row", "revision": 1,
        "proposals": {"items": [{"api_key": "short-secret",
                                   "note": f"Authorization: Bearer {_SECRET}"}]},
    }) + b"\n")

    client = TestClient(make_app(tmp_path))
    claims = client.get("/api/cross-run/claims").json()
    atlas = client.get("/api/cross-run/atlas").json()
    log = client.get("/api/cross-run/claim-curation-log").json()
    rendered = json.dumps({"claims": claims, "atlas": atlas, "log": log})

    assert claims["returned"] == len(claims["claims"]) == 1
    assert claims["claims"][0]["statement"].startswith("password=***")
    assert atlas["explored"][0]["concept"] == "token/sk-***"
    assert log["entries"][0]["proposals"]["items"][0]["api_key"] == "***"
    assert _SECRET not in rendered and "short-secret" not in rendered


def test_steward_nested_payload_is_redacted_before_receipt_persistence(
        tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import looplab.core.llm as llm_module
    import looplab.engine.claim_steward as steward_module
    from looplab.serve.server import make_app

    Path(os.environ["LOOPLAB_MEMORY_DIR"]).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(llm_module, "make_llm_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(steward_module, "steward_claims", lambda *_args, **_kwargs: {
        "proposals": {"reviews": [{"api_key": "short-secret",
                                     "note": f"Authorization: Bearer {_SECRET}"}]},
        "receipt": {"nested": {"access_token": "tiny-secret"}},
    })

    client = TestClient(make_app(tmp_path))
    portfolio_id = client.get("/api/cross-run/claim-curation-log").json()["portfolio_id"]
    response = client.post(
        "/api/cross-run/claim-steward", params={
            "action_id": "nested-security", "expected_portfolio_id": portfolio_id})
    persisted = (Path(os.environ["LOOPLAB_MEMORY_DIR"]) /
                 "claim_curation_log.jsonl").read_text(encoding="utf-8")
    rendered = response.text + persisted

    assert response.status_code == 200
    assert response.json()["proposals"]["reviews"][0]["api_key"] == "***"
    assert response.json()["receipt"]["nested"]["access_token"] == "***"
    assert _SECRET not in rendered and "short-secret" not in rendered and "tiny-secret" not in rendered
