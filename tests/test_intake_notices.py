"""Review 2026-10-14 §8.3: what the intake did to authored text is SAID on the command receipt.

Measured before: findings of 7,200 characters stored as 1,200 plus a hash receipt, a fact stored as
`***REDACTED***` and glued prose — each under a receipt that said `succeeded` and nothing else."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.serve.intake_notices import glued_tokens, intake_notices
from looplab.serve.server import make_app
from tests.factories import post_command

AGENT = {"X-LoopLab-Token": "agent-secret"}

GLUED_RU = "G1измерен, обязательныйG0контроль ещёне завершён. Не превращаем одиночную метрику"
GLUED_EN = "Fixedrecipesall6weeksunchanged and WeeklyAIRIadaptationcutoffsVERIFIEDtoday."
READABLE = [
    "The model after w06 reached 0.231 on `InCart_FilteredUnseenRecall@200` (node #12).",
    "Модель после w06 дала 0.231 на `FilteredUnseenRecall@20`; см. /dev/shm/run/nodes/node_3.",
    "Cache dir 5B_t12_seqprefix_k0mixh50_treeb8 and sha 3f2a9c1b0d4e5f60718293a4b5c6d7e8f9.",
    "https://example.com/a/very/long/path/without/any/spaces/at/all/in/it",
]


def test_the_detector_flags_glued_prose_and_leaves_identifiers_alone():
    assert glued_tokens(GLUED_RU)
    assert glued_tokens(GLUED_EN)
    for text in READABLE:
        assert glued_tokens(text) == [], text


def test_truncation_dropped_items_and_masking_are_reported_with_numbers():
    from looplab.core.advisory_payloads import sanitize_research_memo_payload
    raw = {"summary": "ok", "findings": ["x" * 7200] + [f"finding {i}" for i in range(40)],
           "open_questions": ["Does NewdevexcludesALL35000frozenbenchmarkusers hold?",
                              "token sk-abcdefghijklmnopqrstuvwxyz123456 leaked?"]}
    kept = sanitize_research_memo_payload({**raw, "at_node": 0, "trigger": "external"})
    notices = intake_notices("research_completed", {"memo": raw}, {"memo": kept})
    kinds = {(n["kind"], n.get("field")) for n in notices}
    assert ("truncated", "memo.findings[0]") in kinds
    cut = next(n for n in notices if n["kind"] == "truncated")
    assert cut["original_chars"] == 7200 and 0 < cut["kept_chars"] < 1200
    omitted = next(n for n in notices if n["kind"] == "items_omitted")
    assert omitted == {"kind": "items_omitted", "field": "memo.findings",
                       "submitted": 41, "kept": 32}
    redacted = [n for n in notices if n["kind"] == "redacted"]
    # The glued prose is no longer masked at all (core/redact.py); the real key shape is.
    assert [n["field"] for n in redacted] == ["memo.open_questions[1]"]
    assert redacted[0]["reason"] == "credential_shape"


def test_a_clean_payload_has_no_notices():
    from looplab.core.advisory_payloads import sanitize_report_payload
    raw = {"headline": "Recall improved by 3 %", "verdict": READABLE[0]}
    kept = sanitize_report_payload({**raw, "at_node": 0, "trigger": "external"})
    assert intake_notices("report_generated", {"content": raw}, {"content": kept}) == []
    assert intake_notices("pause", {}, {}) == []


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "operator-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    settings = Settings(backend="toy", external_harness=True, deep_research_every=-1,
                        report_every=0, track_hypotheses=False)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "one", "task_id": "task",
                                 "goal": "g", "direction": "min"})
    store.append("pause", {})
    return TestClient(make_app(tmp_path)), rd


def test_the_command_receipt_carries_the_notices_and_the_event_does_not(tmp_path, monkeypatch):
    client, rd = _client(tmp_path, monkeypatch)
    memo = {"summary": GLUED_EN, "findings": ["y" * 3000, READABLE[1]]}
    response = post_command(client, "research_completed", {"memo": memo}, key="memo-1",
                            headers=AGENT)
    assert response.status_code == 200, response.text
    receipt = response.json()
    notices = receipt.get("intake_notices") or []
    kinds = [n["kind"] for n in notices]
    assert "truncated" in kinds and "readability" in kinds and "readability_hint" in kinds
    glued = next(n for n in notices if n["kind"] == "readability")
    assert glued["field"] == "memo.summary" and glued["samples"]
    events = [json.loads(line) for line in (rd / "events.jsonl").read_text().splitlines()]
    assert not any("intake_notices" in json.dumps(e.get("data")) for e in events)
    # The same receipt is what a later GET of the command returns.
    again = client.get(f"/api/runs/demo/commands/{receipt['id']}", headers=AGENT).json()
    assert again.get("intake_notices") == notices


def test_a_harness_decision_receipt_warns_about_glued_reason(tmp_path, monkeypatch):
    from looplab.events.run_generation import run_generation_token
    from tests.test_harness_handoff import _run
    _rd, store, client = _run(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    headers = {"X-LoopLab-Token": "agent-secret-1234"}
    decision = {"expected_generation": generation, "phase_id": "novelty",
                "action_id": "novelty-1", "idea": {"operator": "draft"},
                "decision": "submit", "reason": GLUED_RU}
    receipt = client.post("/api/runs/demo/harness-decisions", json=decision, headers=headers)
    assert receipt.status_code == 200, receipt.text
    warnings = receipt.json()["readability_warnings"]
    assert warnings[0]["field"] == "reason" and warnings[-1]["kind"] == "readability_hint"
    clean = client.post("/api/runs/demo/harness-decisions", headers=headers, json={
        **decision, "action_id": "novelty-2", "reason": "The idea differs from previous trials"})
    assert clean.status_code == 200 and "readability_warnings" not in clean.json()
