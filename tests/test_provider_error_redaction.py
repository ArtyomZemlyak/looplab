"""Owner API provider failures never reflect transport URLs, credentials, or exception payloads."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.core.models import Event  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.serve.assistant import safe_provider_failure  # noqa: E402
from looplab.serve.report import generate_report  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


_LEAK = (
    "HTTP 401 request failed at "
    "https://api-user:api-secret@provider.example/v1/chat?user_id=private-user&token=hidden"
)
_FORBIDDEN = (
    "api-user",
    "api-secret",
    "provider.example",
    "user_id",
    "private-user",
    "token=hidden",
    "/v1/chat",
)


def _assert_safe(payload) -> None:
    rendered = payload if isinstance(payload, str) else json.dumps(payload)
    for fragment in _FORBIDDEN:
        assert fragment not in rendered


def _provider_boom(*_args, **_kwargs):
    raise RuntimeError(_LEAK)


def _minimal_run(root, name: str = "demo") -> None:
    rd = root / name
    rd.mkdir()
    EventStore(rd / "events.jsonl").append("run_started", {
        "run_id": name,
        "task_id": "provider-redaction",
        "goal": "minimize loss",
        "direction": "min",
    })


def test_safe_provider_failure_is_allow_listed():
    failure = safe_provider_failure(RuntimeError(_LEAK))
    assert failure["error_kind"] == "credentials"
    assert failure["error"] == failure["message"]
    assert "credentials" in failure["message"].lower()
    _assert_safe(failure)


def test_safe_provider_failure_preserves_only_the_generation_conflict_code():
    from fastapi import HTTPException

    failure = safe_provider_failure(HTTPException(409, {
        "code": "run_generation_changed",
        "message": _LEAK,
    }))
    assert failure == {
        "error": "run_generation_changed",
        "error_kind": "run_state_conflict",
        "message": "The run was reset or replaced before this work started.",
    }
    _assert_safe(failure)


def test_boss_cost_accounting_failure_is_not_misreported_as_provider_outage():
    from looplab.serve.routers.boss import _RunCostAccountingPending, _safe_boss_failure

    failure = _safe_boss_failure(_RunCostAccountingPending())

    assert failure["error_kind"] == "accounting_pending"
    assert "durable cost accounting" in failure["message"]
    _assert_safe(failure)


def test_direct_boss_route_preserves_sanitized_generation_conflict(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from looplab.serve.routers import boss as boss_router

    _minimal_run(tmp_path)

    def stale_generation(*_args, **_kwargs):
        raise HTTPException(409, {"code": "run_generation_changed", "message": _LEAK})

    monkeypatch.setattr(boss_router, "_metered_run_client", stale_generation)
    response = TestClient(make_app(tmp_path)).post(
        "/api/runs/demo/chat", json={"messages": [{"role": "user", "content": "status"}]})

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "run_generation_changed",
        "message": "The run was reset or replaced before this work started.",
        "remediation": "Reload the current run generation before trying again.",
    }
    _assert_safe(response.json())


def test_background_boss_route_maps_only_allow_listed_domain_detail(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from looplab.serve.routers import boss as boss_router

    _minimal_run(tmp_path)

    def stale_generation(*_args, **_kwargs):
        raise HTTPException(409, {"code": "run_generation_changed", "message": _LEAK})

    monkeypatch.setattr(boss_router, "_metered_run_client", stale_generation)
    response = TestClient(make_app(tmp_path)).post(
        "/api/runs/demo/command", json={"instruction": "what next?"})
    body = response.json()

    assert response.status_code == 200
    assert body == {
        "ok": False,
        "code": "run_generation_changed",
        "error_kind": "run_state_conflict",
        "error": "The run was reset or replaced before this work started.",
    }
    _assert_safe(body)


def test_llm_health_never_reflects_configured_base_url_or_exception(tmp_path, monkeypatch):
    """The probe is a revision-fenced POST, not the old free GET — a paid, at-most-once provider
    call bound to the saved Settings snapshot. This test travels with that contract because the
    property it guards is about the RESPONSE, not the verb: a provider failure must never reflect
    the configured base URL, its embedded credentials, or the raw exception text. Sending the old
    GET made this route 404, which silently retired the redaction check rather than failing it."""
    monkeypatch.setenv(
        "LOOPLAB_LLM_BASE_URL",
        "https://config-user:config-secret@provider.example/v1?token=config-token",
    )
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _provider_boom)
    client = TestClient(make_app(tmp_path))
    snapshot = client.get("/api/settings").json()
    response = client.post("/api/llm/health", json={
        "expected_settings_revision": snapshot["settings_revision"],
        "expected_secret_revision": snapshot["secret_revision"],
        "operation_id": "b2f6c1de-4a3e-4c1b-9f57-0d1e2a3b4c5d",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    # The failure now surfaces from CLIENT CONSTRUCTION (the preflight), which is not the provider
    # declining the request — so it is classified as a generic provider error rather than the
    # `credentials` an explicit 401 rejection would carry. Pin the safe SET, not one member: the
    # redaction contract is that the kind is drawn from the allow-list at all.
    assert body["error_kind"] in {"credentials", "provider_error", "unavailable", "rate_limit"}
    assert "base_url" not in body
    _assert_safe(body)
    for secret in ("config-secret", "config-user", "config-token", "provider.example"):
        assert secret not in json.dumps(body)


def test_research_provider_failure_is_redacted(tmp_path, monkeypatch):
    # Patched at `looplab.serve.server.make_llm_client` — the documented single seam every router
    # resolves late through (AppState.make_llm_client). /api/research used to import the factory
    # straight from adapters.tasks, so it missed this patch point AND the canonical settings path.
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _provider_boom)
    response = TestClient(make_app(tmp_path)).post(
        "/api/research", json={"topic": "bounded security review"})
    body = response.json()

    assert response.status_code == 200 and body["ok"] is False
    assert body["error_kind"] == "credentials" and "base_url" not in body
    _assert_safe(body)


def test_genesis_provider_creation_failure_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _provider_boom)
    response = TestClient(make_app(tmp_path)).post(
        "/api/genesis", json={"instruction": "plan a small run"})
    body = response.json()

    assert response.status_code == 200 and body["ok"] is False
    assert body["error_kind"] == "credentials" and body["spec"]["run_id"] == ""
    _assert_safe(body)


def test_genesis_planning_failure_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _settings: object())
    monkeypatch.setattr("looplab.core.parse.parse_structured", _provider_boom)
    response = TestClient(make_app(tmp_path)).post(
        "/api/genesis", json={"instruction": "plan a small run"})
    body = response.json()

    assert response.status_code == 200 and body["ok"] is False
    assert body["error_kind"] == "credentials"
    _assert_safe(body)


@pytest.mark.parametrize(("path", "body"), [
    ("/api/runs/demo/chat-compact", {"messages": [{"role": "user", "content": "recap"}]}),
    ("/api/runs/demo/chat", {"messages": [{"role": "user", "content": "status"}]}),
    ("/api/runs/demo/suggest", {"instruction": "try another feature"}),
    ("/api/runs/demo/command", {"instruction": "what next?"}),
    ("/api/runs/demo/report_refresh", {}),
])
def test_boss_provider_failures_are_redacted(tmp_path, monkeypatch, path, body):
    _minimal_run(tmp_path)
    monkeypatch.setattr("looplab.serve.server.make_llm_client", _provider_boom)
    client = TestClient(make_app(tmp_path))
    headers = {}
    if path.endswith("/report_refresh"):
        body = {"expected_generation": client.get("/api/runs/demo/state").json()["generation"]}
        headers["Idempotency-Key"] = "provider-redaction-report"
    response = client.post(path, json=body, headers=headers)
    result = response.json()

    assert response.status_code == 200 and result["ok"] is False
    assert result["error_kind"] == "credentials"
    _assert_safe(result)


def test_assistant_background_sse_failure_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _settings: object())
    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", _provider_boom)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "plan"}).json()["id"]
    response = client.post(
        f"/api/assistant/sessions/{sid}/message_stream",
        json={"instruction": "inspect the run", "mode": "plan"},
    )

    assert response.status_code == 200 and "event: error" in response.text
    assert "credentials" in response.text.lower()
    _assert_safe(response.text)


def test_report_fallback_does_not_persist_provider_exception(tmp_path, monkeypatch):
    def _agentic_boom(*_args, **_kwargs):
        raise RuntimeError(_LEAK)

    monkeypatch.setattr("looplab.agents.agent.agentic_struct", _agentic_boom)
    state = fold([Event(
        seq=0,
        type="run_started",
        data={"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"},
    )])
    report = generate_report(state, client=object(), trigger="manual")

    assert report["headline"] == "(report unavailable)"
    assert "provider" in report["verdict"].lower()
    _assert_safe(report)


def test_report_action_chat_log_canonicalizes_legacy_transport_error(tmp_path):
    _minimal_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    turn = {
        "role": "action",
        "action": {"type": "__refresh_report__", "label": "refresh"},
        "status": "failed",
        "error": f"could not reach {_LEAK}",
    }

    stored = client.post("/api/runs/demo/chat-log", json=turn)
    projected = client.get("/api/runs/demo/chat-log")

    assert stored.status_code == 200 and projected.status_code == 200
    raw = (tmp_path / "demo" / "chat.jsonl").read_text(encoding="utf-8")
    _assert_safe(raw)
    _assert_safe(projected.text)
    assert projected.json()[0]["error_kind"] == "credentials"


def test_report_timeout_terminal_replays_sanitized_kind_without_a_second_call(
        tmp_path, monkeypatch):
    _minimal_run(tmp_path)
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _settings: object())
    calls = []

    def timeout(*_args, **_kwargs):
        calls.append("paid")
        raise TimeoutError(
            "network timeout while calling "
            "https://provider.example/v1/chat?trace=private-user"
        )

    monkeypatch.setattr("looplab.serve.report.generate_report", timeout)
    first_client = TestClient(make_app(tmp_path))
    generation = first_client.get("/api/runs/demo/state").json()["generation"]
    request = {
        "headers": {"Idempotency-Key": "durable-timeout"},
        "json": {"expected_generation": generation},
    }

    first = first_client.post("/api/runs/demo/report_refresh", **request).json()

    assert first["ok"] is False and first["error_kind"] == "unavailable"
    _assert_safe(first)
    terminals = [
        event for event in EventStore(tmp_path / "demo" / "events.jsonl").read_all()
        if event.type == "report_refresh_failed"
    ]
    assert len(terminals) == 1 and terminals[0].data["error_kind"] == "unavailable"

    def forbidden(_settings):
        raise AssertionError("a durable terminal receipt must not start a second provider call")

    monkeypatch.setattr("looplab.serve.server.make_llm_client", forbidden)
    replayed = TestClient(make_app(tmp_path)).post(
        "/api/runs/demo/report_refresh", **request).json()

    assert replayed["ok"] is False and replayed["error_kind"] == "unavailable"
    assert calls == ["paid"]
    _assert_safe(replayed)


def test_research_uses_the_canonical_llm_settings_path(tmp_path, monkeypatch):
    """/api/research must resolve its client the way every other LLM endpoint does.

    It hand-filtered `load_ui_settings()` down to four keys, which dropped the connection-PROFILE
    fields — so a model configured through `llm_profile` silently fell back to the bare default —
    and it skipped `srv.llm_settings`, and with it `store.refresh_env_secrets()`: a key saved after
    server start worked on /genesis but could fail here. `llm_api_key` was in that filter but is
    never in ui_settings at all (secrets live in the secret store), so it never did anything."""
    seen = {}

    def _capture(settings, *_args, **_kwargs):
        seen["model"] = settings.llm_model
        seen["profile"] = settings.llm_profile
        raise RuntimeError("offline")

    monkeypatch.setattr("looplab.serve.server.make_llm_client", _capture)
    app = make_app(tmp_path)
    app.state.looplab.settings.write_ui_settings({
        "llm_profile": "fast",
        "llm_profiles": {"fast": {"model": "profile-model", "base_url": "http://p/v1"}},
    })

    body = TestClient(app).post("/api/research", json={"topic": "t"}).json()
    assert body["ok"] is False
    # The profile reaches the client factory, which is what expands it into the real model.
    assert seen["profile"] == "fast", seen
