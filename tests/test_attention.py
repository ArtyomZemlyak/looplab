"""Owner attention feed: event identity, redaction, lifecycle truth, and auth boundaries."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.types import (  # noqa: E402
    EV_APPROVAL_GRANTED,
    EV_APPROVAL_REQUESTED,
    EV_FINALIZATION_FINISHED,
    EV_NODE_CREATED,
    EV_NODE_ABORT,
    EV_NODE_EVALUATED,
    EV_NODE_FAILED,
    EV_RESUME_REQUESTED,
    EV_RUN_FINISHED,
    EV_RUN_STARTED,
    EV_SPEC_APPROVAL_REQUESTED,
    EV_SPEC_PROPOSED,
)
from looplab.serve.attention import project_run_attention  # noqa: E402
from looplab.serve.run_commands import run_generation_token  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


def _store(root: Path, run_id: str = "demo", *, goal: str = "safe goal") -> EventStore:
    rd = root / run_id
    rd.mkdir(parents=True, exist_ok=True)
    store = EventStore(rd / "events.jsonl")
    store.append(EV_RUN_STARTED, {
        "run_id": run_id, "task_id": "task", "goal": goal, "direction": "min",
    })
    return store


def _node(store: EventStore, node_id: int, *, generation: int = 0,
          metric: float | None = None, failed: bool = False,
          error: str = "candidate failed") -> None:
    store.append(EV_NODE_CREATED, {
        "node_id": node_id, "generation": generation, "parent_ids": [],
        "operator": "draft", "idea": {"operator": "draft", "rationale": "test"},
    })
    if failed:
        store.append(EV_NODE_FAILED, {
            "node_id": node_id, "generation": generation, "reason": "crash",
            "error": error, "eval_seconds": 0.1,
        })
    elif metric is not None:
        store.append(EV_NODE_EVALUATED, {
            "node_id": node_id, "generation": generation, "metric": metric,
            "eval_seconds": 0.1,
        })


def _kinds(items: list[dict]) -> set[str]:
    return {item["kind"] for item in items}


def test_result_approval_uses_exact_pending_subject_and_generation(tmp_path):
    store = _store(tmp_path)
    _node(store, 7, generation=4, metric=10.0)
    _node(store, 3, generation=0, metric=1.0)  # current best differs from requested subject
    request = store.append(EV_APPROVAL_REQUESTED, {"node_id": 7, "generation": 4})

    first = project_run_attention("demo", store.read_all(), engine_running=False)
    approval = next(item for item in first if item["kind"] == "approval")
    assert approval["node_id"] == 7 and approval["node_generation"] == 4
    assert approval["seq"] == request.seq and approval["browser"] is True
    assert len(approval["id"]) == 64 and "demo" not in approval["id"]

    # An unrelated append cannot change the causal notification identity.
    store.append("diagnostic_only", {"secret": "must-not-be-projected"})
    second = project_run_attention("demo", store.read_all(), engine_running=False)
    assert next(item for item in second if item["kind"] == "approval")["id"] == approval["id"]

    # A duplicate or invalid CAS request that replay ignores cannot rotate the causal identity.
    store.append(EV_APPROVAL_REQUESTED, {
        "node_id": 7, "generation": 4, "after_seq": request.seq - 20,
    })
    store.append(EV_APPROVAL_REQUESTED, {"node_id": 7, "generation": 4})
    repeated = project_run_attention("demo", store.read_all(), engine_running=False)
    assert next(item for item in repeated if item["kind"] == "approval")["id"] == approval["id"]

    copied = project_run_attention("copied-directory", store.read_all(), engine_running=False)
    assert next(item for item in copied if item["kind"] == "approval")["id"] != approval["id"]

    store.append(EV_APPROVAL_GRANTED, {"node_id": 7, "generation": 4})
    assert "approval" not in _kinds(project_run_attention(
        "demo", store.read_all(), engine_running=False))


def test_incomplete_approval_fails_closed_and_spec_request_is_distinct(tmp_path):
    store = _store(tmp_path)
    # A legacy subject-less request folds as awaiting approval but cannot create a guessed command.
    store.append(EV_APPROVAL_REQUESTED, {})
    items = project_run_attention("demo", store.read_all(), engine_running=False)
    incomplete = next(item for item in items if item["kind"] == "approval_incomplete")
    assert incomplete["browser"] is False and "node_id" not in incomplete

    spec = _store(tmp_path, "spec")
    spec.append(EV_SPEC_PROPOSED, {"eval_spec": {"metric": {"kind": "builtin"}}})
    spec.append(EV_SPEC_APPROVAL_REQUESTED, {"eval": {"private": "not projected"}})
    spec_item = next(item for item in project_run_attention(
        "spec", spec.read_all(), engine_running=False) if item["kind"] == "spec_approval")
    assert spec_item["active"] is True and spec_item["browser"] is True
    assert "private" not in json.dumps(spec_item)

    malformed = _store(tmp_path, "malformed-spec")
    malformed.append(EV_SPEC_APPROVAL_REQUESTED, {})
    assert "spec_approval" not in _kinds(project_run_attention(
        "malformed-spec", malformed.read_all(), engine_running=False))


def test_failure_spike_is_thresholded_and_payload_is_redacted(tmp_path):
    secret = "sk-attention-secret-must-never-leak-123456789"
    store = _store(tmp_path, goal=secret)
    for node_id in range(5):
        _node(store, node_id, failed=True, error=secret)
    items = project_run_attention("demo", store.read_all(), engine_running=False)
    spike = next(item for item in items if item["kind"] == "failure_spike")
    assert spike["active"] is True and spike["browser"] is True
    assert secret not in json.dumps(items)

    prior_id = spike["id"]
    _node(store, 5, failed=True, error=secret)
    latest = next(item for item in project_run_attention(
        "demo", store.read_all(), engine_running=False) if item["kind"] == "failure_spike")
    assert latest["id"] != prior_id  # the sixth accepted current failure starts the next group

    store.append(EV_NODE_FAILED, {
        "node_id": 5, "generation": 0, "reason": "crash", "error": secret,
    })
    duplicate = next(item for item in project_run_attention(
        "demo", store.read_all(), engine_running=False) if item["kind"] == "failure_spike")
    assert duplicate["id"] == latest["id"]


def test_failure_spike_does_not_rebucket_old_failures_after_abort(tmp_path):
    store = _store(tmp_path)
    for node_id in range(4):
        _node(store, node_id, failed=True)
    before = next(item for item in project_run_attention(
        "demo", store.read_all(), engine_running=False) if item["kind"] == "failure_spike")

    store.append(EV_NODE_ABORT, {"node_id": 0, "generation": 0})
    after = next(item for item in project_run_attention(
        "demo", store.read_all(), engine_running=False) if item["kind"] == "failure_spike")
    assert after["id"] == before["id"]

    # A duplicate terminal is ignored by replay and likewise cannot create another crossing.
    store.append(EV_NODE_FAILED, {
        "node_id": 3, "generation": 0, "reason": "crash", "error": "duplicate",
    })
    repeated = next(item for item in project_run_attention(
        "demo", store.read_all(), engine_running=False) if item["kind"] == "failure_spike")
    assert repeated["id"] == before["id"]


def test_modern_finish_waits_for_marker_and_driver_release(tmp_path):
    store = _store(tmp_path)
    finish = store.append(EV_RUN_FINISHED, {"finalization_required": True})
    assert "finished" not in _kinds(project_run_attention(
        "demo", store.read_all(), engine_running=False))
    store.append(EV_FINALIZATION_FINISHED, {"finish_seq": finish.seq})
    assert "finished" not in _kinds(project_run_attention(
        "demo", store.read_all(), engine_running=True))
    finished = next(item for item in project_run_attention(
        "demo", store.read_all(), engine_running=False) if item["kind"] == "finished")
    assert finished["detail"] == "The final report and durable wrap-up are ready."
    store.append(EV_FINALIZATION_FINISHED, {"finish_seq": finish.seq})
    duplicate_marker = next(item for item in project_run_attention(
        "demo", store.read_all(), engine_running=False) if item["kind"] == "finished")
    assert duplicate_marker["id"] == finished["id"]

    budget = _store(tmp_path, "budget")
    budget_finish = budget.append(EV_RUN_FINISHED, {
        "reason": "eval_budget", "finalization_required": True,
    })
    budget.append(EV_FINALIZATION_FINISHED, {"finish_seq": budget_finish.seq})
    budget_items = project_run_attention("budget", budget.read_all(), engine_running=False)
    assert _kinds(budget_items) == {"budget_exhausted"}

    failed = _store(tmp_path, "failed", goal="TOP-SECRET-GOAL")
    failed.append(EV_RUN_FINISHED, {
        "reason": "error", "error": "TOP-SECRET-ERROR", "finalization_required": True,
    })
    failure = next(item for item in project_run_attention(
        "failed", failed.read_all(), engine_running=True) if item["kind"] == "run_failed")
    assert "TOP-SECRET" not in json.dumps(failure)

    no_candidate = _store(tmp_path, "no-candidate")
    no_candidate.append(EV_RUN_FINISHED, {"reason": "no_eligible_candidate"})
    no_candidate_item = next(item for item in project_run_attention(
        "no-candidate", no_candidate.read_all(), engine_running=True)
        if item["kind"] == "run_failed")
    assert no_candidate_item["severity"] == "danger" and no_candidate_item["active"] is True

    leakage = _store(tmp_path, "leakage")
    leakage.append(EV_RUN_FINISHED, {"reason": "leakage", "finalization_required": True})
    leakage_item = next(item for item in project_run_attention(
        "leakage", leakage.read_all(), engine_running=True) if item["kind"] == "run_failed")
    assert leakage_item["severity"] == "danger" and leakage_item["browser"] is True

    stopped = _store(tmp_path, "stopped")
    stopped_finish = stopped.append(EV_RUN_FINISHED, {
        "reason": "aborted", "finalization_required": True,
    })
    assert "stopped" not in _kinds(project_run_attention(
        "stopped", stopped.read_all(), engine_running=False))
    stopped.append(EV_FINALIZATION_FINISHED, {"finish_seq": stopped_finish.seq})
    stopped_item = next(item for item in project_run_attention(
        "stopped", stopped.read_all(), engine_running=False) if item["kind"] == "stopped")
    assert stopped_item["browser"] is False and stopped_item["active"] is False
    assert "finished" not in _kinds(project_run_attention(
        "stopped", stopped.read_all(), engine_running=False))

    leakage = _store(tmp_path, "leakage", goal="PRIVATE-GOAL")
    leakage.append(EV_RUN_FINISHED, {
        "reason": "leakage", "error": "PRIVATE-LEAKAGE-EVIDENCE",
        "finalization_required": True,
    })
    leakage_item = next(item for item in project_run_attention(
        "leakage", leakage.read_all(), engine_running=True) if item["kind"] == "run_failed")
    assert leakage_item["active"] is True and leakage_item["browser"] is True
    assert "PRIVATE" not in json.dumps(leakage_item)

    stopped = _store(tmp_path, "stopped")
    stopped_finish = stopped.append(EV_RUN_FINISHED, {
        "reason": "aborted", "finalization_required": True,
    })
    assert "stopped" not in _kinds(project_run_attention(
        "stopped", stopped.read_all(), engine_running=False))
    stopped.append(EV_FINALIZATION_FINISHED, {"finish_seq": stopped_finish.seq})
    assert "stopped" not in _kinds(project_run_attention(
        "stopped", stopped.read_all(), engine_running=True))
    stopped_item = next(item for item in project_run_attention(
        "stopped", stopped.read_all(), engine_running=False) if item["kind"] == "stopped")
    assert stopped_item["browser"] is False and stopped_item["active"] is False


def test_finalization_stall_waits_for_grace_and_skips_pending_handoff(tmp_path):
    store = _store(tmp_path)
    store.append(EV_RUN_FINISHED, {"finalization_required": True})
    tail = store.read_all()[-1]
    assert "finalization_stalled" not in _kinds(project_run_attention(
        "demo", store.read_all(), engine_running=False, now=tail.ts + 5))
    assert "finalization_stalled" in _kinds(project_run_attention(
        "demo", store.read_all(), engine_running=False, now=tail.ts + 16))
    store.append(EV_RESUME_REQUESTED, {"mode": "finalize"})
    handoff_tail = store.read_all()[-1]
    assert "finalization_stalled" not in _kinds(project_run_attention(
        "demo", store.read_all(), engine_running=False, now=handoff_tail.ts + 16))


def test_stall_is_derived_in_app_only_and_generation_changes_on_replacement(tmp_path):
    store = _store(tmp_path)
    first_generation = run_generation_token(store.read_all())
    first_now = store.read_all()[-1].ts + 16
    stalled = next(item for item in project_run_attention(
        "demo", store.read_all(), engine_running=False, now=first_now) if item["kind"] == "stalled")
    assert stalled["derived"] is True and stalled["browser"] is False
    assert "stalled" not in _kinds(project_run_attention(
        "demo", store.read_all(), engine_running=None, now=first_now))

    log = tmp_path / "demo" / "events.jsonl"
    log.replace(tmp_path / "demo" / "events.previous.jsonl")
    replacement = _store(tmp_path)
    second_generation = run_generation_token(replacement.read_all())
    assert second_generation != first_generation
    replacement_now = replacement.read_all()[-1].ts + 16
    replacement_stall = next(item for item in project_run_attention(
        "demo", replacement.read_all(), engine_running=False,
        now=replacement_now) if item["kind"] == "stalled")
    assert replacement_stall["id"] != stalled["id"]


def test_attention_endpoint_is_owner_only_no_store_bounded_and_observation_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    store = _store(tmp_path)
    _node(store, 0, metric=1.0)
    store.append(EV_APPROVAL_REQUESTED, {"node_id": 0, "generation": 0})
    finished_store = _store(tmp_path, "finished")
    finished_store.append(EV_RUN_FINISHED, {"reason": "done"})
    (tmp_path / "demo" / "engine.lock").write_bytes(b"existing-lock-sentinel")
    app = make_app(tmp_path)
    client = TestClient(app)

    before = (tmp_path / "demo" / "events.jsonl").read_bytes()
    before_tree = {
        path.relative_to(tmp_path / "demo").as_posix(): path.read_bytes()
        for path in (tmp_path / "demo").rglob("*") if path.is_file()
    }
    denied = client.get("/api/attention")
    assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
    owner = {"X-LoopLab-Token": "owner-secret"}
    response = client.get("/api/attention?limit=1", headers=owner)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["schema"] == 1 and len(response.json()["items"]) == 1
    assert response.json()["partial"] is False and response.json()["truncated"] is True
    cursor = response.json()["next_cursor"]
    second_page = client.get(f"/api/attention?limit=1&cursor={cursor}", headers=owner)
    assert second_page.status_code == 200 and len(second_page.json()["items"]) == 1
    assert second_page.json()["items"][0]["id"] != response.json()["items"][0]["id"]
    assert (tmp_path / "demo" / "events.jsonl").read_bytes() == before
    assert {
        path.relative_to(tmp_path / "demo").as_posix(): path.read_bytes()
        for path in (tmp_path / "demo").rglob("*") if path.is_file()
    } == before_tree
    assert not (tmp_path / ".command-locks").exists()

    invalid = client.get("/api/attention?limit=0", headers=owner)
    assert invalid.status_code == 422 and invalid.headers["cache-control"] == "no-store"
    stale_cursor = client.get(f"/api/attention?cursor={'0' * 64}", headers=owner)
    assert stale_cursor.status_code == 409 and stale_cursor.headers["cache-control"] == "no-store"
    assert client.post("/api/attention", headers=owner).status_code == 405

    generation = run_generation_token(store.read_all())
    token, _record = app.state.looplab.reviews.create("demo", generation=generation)
    review = {"X-LoopLab-Review": token}
    assert client.get("/api/attention", headers=review).status_code == 403
    assert client.get("/api/attention", headers={**owner, **review}).status_code == 403


def test_attention_endpoint_marks_complete_log_corruption_partial_and_fails_closed(tmp_path):
    good = _store(tmp_path, "good")
    _node(good, 0, metric=1.0)
    good.append(EV_APPROVAL_REQUESTED, {"node_id": 0, "generation": 0})

    damaged = _store(tmp_path, "damaged")
    _node(damaged, 1, metric=1.0)
    damaged.append(EV_APPROVAL_REQUESTED, {"node_id": 1, "generation": 0})
    terminal = _store(tmp_path, "terminal")
    terminal.append(EV_RUN_FINISHED, {"reason": "done"})
    client = TestClient(make_app(tmp_path))
    warm = client.get("/api/attention").json()
    prior = next(item for item in warm["items"] if item["run_id"] == "damaged")
    prior_terminal = next(item for item in warm["items"] if item["run_id"] == "terminal")
    with (tmp_path / "damaged" / "events.jsonl").open("ab") as handle:
        handle.write(b"not-a-json-event\n")
    with (tmp_path / "terminal" / "events.jsonl").open("ab") as handle:
        handle.write(b"not-a-json-event\n")

    response = client.get("/api/attention")
    assert response.status_code == 200
    payload = response.json()
    assert payload["partial"] is True
    assert {item["run_id"] for item in payload["items"]} == {"good", "damaged", "terminal"}
    stale = next(item for item in payload["items"] if item["run_id"] == "damaged")
    assert stale["id"] == prior["id"] and stale["stale"] is True and stale["browser"] is False
    stale_terminal = next(item for item in payload["items"] if item["run_id"] == "terminal")
    assert stale_terminal["id"] == prior_terminal["id"]
    assert stale_terminal["stale"] is True and stale_terminal["browser"] is False


def test_attention_endpoint_preserves_snapshot_when_run_root_disappears(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    app = make_app(root)
    root.rename(tmp_path / "runs-offline")
    response = TestClient(app).get("/api/attention")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


def test_attention_endpoint_keeps_last_safe_snapshot_during_log_replacement(tmp_path):
    store = _store(tmp_path)
    _node(store, 0, metric=1.0)
    store.append(EV_APPROVAL_REQUESTED, {"node_id": 0, "generation": 0})
    client = TestClient(make_app(tmp_path))
    prior = client.get("/api/attention").json()["items"][0]

    (tmp_path / "demo" / "events.jsonl").replace(tmp_path / "demo" / "events.replacing")
    payload = client.get("/api/attention").json()
    assert payload["partial"] is True
    assert len(payload["items"]) == 1
    assert payload["items"][0]["id"] == prior["id"]
    assert payload["items"][0]["stale"] is True
    assert payload["items"][0]["browser"] is False


def test_attention_cache_does_not_thrash_above_old_512_run_boundary(tmp_path, monkeypatch):
    from looplab.serve.routers import attention as attention_router

    for index in range(513):
        rd = tmp_path / f"run-{index:04d}"
        rd.mkdir()
        record = {
            "v": 1, "seq": 0, "ts": 9_999_999_999.0, "type": EV_RUN_STARTED,
            "data": {"run_id": rd.name, "task_id": "task", "goal": "g", "direction": "min"},
            "trace_id": None, "span_id": None,
        }
        (rd / "events.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    calls = 0
    original = attention_router.project_event_attention

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(attention_router, "project_event_attention", counted)
    client = TestClient(make_app(tmp_path))
    assert client.get("/api/attention?limit=1").status_code == 200
    assert calls == 513
    assert client.get("/api/attention?limit=1").status_code == 200
    assert calls == 513  # unchanged second poll performs zero re-folds
