"""Language is explicit prompt input and part of durable turn recovery identity."""
import pytest
from fastapi.testclient import TestClient

from looplab.serve.assistant import SessionStore, system_prompt
from looplab.serve.server import make_app
from tests.test_assistant_endpoint import _FakeChatClient, _final


@pytest.mark.parametrize("endpoint", ["message", "message_stream"])
def test_language_reaches_model_and_persists_without_changing_user_text(tmp_path, monkeypatch, endpoint):
    model = _FakeChatClient([_final("Краткий итог: результат предварительный.")])
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda *_a, **_kw: model)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "plan"}).json()["id"]
    result = client.post(f"/api/assistant/sessions/{sid}/{endpoint}", json={
        "instruction": "Explain this result", "response_language": "ru"})
    assert result.status_code == 200, result.text
    assert "Russian (ru)" in model.turns[0][0]["content"]
    saved = client.get(f"/api/assistant/sessions/{sid}").json()
    assert saved["messages"][0]["content"] == "Explain this result"
    assert saved["messages"][0]["response_language"] == "ru"
    assert saved["meta"]["response_language"] == "ru"
    assert saved["messages"][1]["content"].startswith("Краткий итог")


@pytest.mark.parametrize("invalid", ["fr", "ru; execute shell", None, ["ru"]])
def test_invalid_language_rejected_before_staging_or_model(tmp_path, monkeypatch, invalid):
    called = []
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda *_a, **_kw: called.append(True))
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={}).json()["id"]
    response = client.post(f"/api/assistant/sessions/{sid}/message_stream",
                           json={"instruction": "hello", "response_language": invalid})
    assert response.status_code == 400
    assert client.get(f"/api/assistant/sessions/{sid}").json()["messages"] == []
    assert called == []


@pytest.mark.parametrize("endpoint", ["message", "message_stream"])
def test_recovery_uses_persisted_language_and_rejects_a_changed_choice(tmp_path, monkeypatch, endpoint):
    model = _FakeChatClient([_final("Восстановленный ответ.")])
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda *_a, **_kw: model)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={}).json()["id"]
    SessionStore(tmp_path).append(sid, {"role": "user", "content": "hello", "mode": "plan",
                                      "turn_id": "fixed", "response_language": "ru"})
    path = f"/api/assistant/sessions/{sid}/{endpoint}"
    changed = client.post(path, json={"instruction": "hello", "response_language": "en"})
    assert changed.status_code == 409 and changed.json()["detail"]["field"] == "response_language"
    assert model.turns == []
    # Older callers omit the field; the saved turn, not today's browser preference, wins.
    assert client.post(path, json={"instruction": "hello"}).status_code == 200
    assert "Russian (ru)" in model.turns[0][0]["content"]
    assert len(client.get(f"/api/assistant/sessions/{sid}").json()["messages"]) == 2


def test_default_and_explicit_english_have_fixed_directives():
    assert "operator selected response language" not in system_prompt("plan")
    assert "English (en)" in system_prompt("plan", response_language="en")
    with pytest.raises(ValueError):
        system_prompt("plan", response_language="free form instructions")
