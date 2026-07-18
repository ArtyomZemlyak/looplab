"""Secure UI secret store (the LLM API key). PUT /api/settings/secret writes an owner-only
secrets.json (never ui_settings.json / a run snapshot), applies the value to the process env so a
spawned engine inherits it, and the API only ever echoes the masked "***". Skipped without [ui]."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.server import make_app  # noqa: E402
from looplab.serve.settings_store import _REVISION_KEY  # noqa: E402


@pytest.fixture
def _restore_key():
    """The server applies the secret to the real process env; snapshot + restore so it can't leak
    into other tests (the conftest disables .env, but os.environ is still read by Settings())."""
    had, prev = "LOOPLAB_LLM_API_KEY" in os.environ, os.environ.get("LOOPLAB_LLM_API_KEY")
    try:
        yield
    finally:
        if had:
            os.environ["LOOPLAB_LLM_API_KEY"] = prev
        else:
            os.environ.pop("LOOPLAB_LLM_API_KEY", None)


def test_secret_stored_masked_and_applied(tmp_path, _restore_key):
    client = TestClient(make_app(tmp_path))

    r = client.put("/api/settings/secret", json={"key": "llm_api_key", "value": "sk-secret-123"})
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["key"] == "llm_api_key" and r.json()["set"] is True
    assert isinstance(r.json()["secret_revision"], str) and r.json()["secret_revision"]

    # persisted to the dedicated owner-only file, never ui_settings.json
    stored = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert stored == {"llm_api_key": "sk-secret-123", _REVISION_KEY: r.json()["secret_revision"]}
    if (tmp_path / "ui_settings.json").exists():
        assert "llm_api_key" not in json.loads((tmp_path / "ui_settings.json").read_text(encoding="utf-8"))

    # applied to the process env (so a spawned engine inherits it)
    assert os.environ.get("LOOPLAB_LLM_API_KEY") == "sk-secret-123"

    # the API echoes the secret ONLY as the mask — never the value
    settings = client.get("/api/settings").json()["settings"]
    assert settings["llm_api_key"] == "***"
    assert "sk-secret-123" not in json.dumps(settings)


def test_secret_clear_and_reject_unknown(tmp_path, _restore_key):
    client = TestClient(make_app(tmp_path))
    client.put("/api/settings/secret", json={"key": "llm_api_key", "value": "sk-zzz"})
    assert os.environ.get("LOOPLAB_LLM_API_KEY") == "sk-zzz"

    # an empty value clears it from both the store and the env
    r = client.put("/api/settings/secret", json={"key": "llm_api_key", "value": ""})
    assert r.json()["set"] is False
    assert json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8")) == {
        _REVISION_KEY: r.json()["secret_revision"]}
    assert "LOOPLAB_LLM_API_KEY" not in os.environ
    assert client.get("/api/settings").json()["settings"]["llm_api_key"] is None

    # an unknown secret key is rejected
    assert client.put("/api/settings/secret", json={"key": "nope", "value": "x"}).status_code == 400


def test_secret_cas_rejects_old_delayed_write_without_leaking_values(tmp_path, _restore_key):
    client = TestClient(make_app(tmp_path))
    loaded = client.get("/api/settings").json()
    old_revision = loaded["secret_revision"]

    newer = client.put("/api/settings/secret", json={
        "key": "llm_api_key", "value": "sk-newer-value", "expected_revision": old_revision,
    })
    assert newer.status_code == 200
    new_revision = newer.json()["secret_revision"]
    assert new_revision != old_revision

    delayed = client.put("/api/settings/secret", json={
        "key": "llm_api_key", "value": "sk-old-delayed", "expected_revision": old_revision,
    })
    assert delayed.status_code == 409
    detail = delayed.json()["detail"]
    assert detail["code"] == "secret_revision_conflict"
    assert detail["resource"] == "secret"
    assert detail["expected_revision"] == old_revision
    assert detail["current_revision"] == new_revision
    assert "sk-newer-value" not in json.dumps(delayed.json())
    assert "sk-old-delayed" not in json.dumps(delayed.json())
    stored = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert stored["llm_api_key"] == "sk-newer-value"
    assert stored[_REVISION_KEY] == new_revision
    current = client.get("/api/settings").json()
    assert current["secret_revision"] == new_revision
    assert current["settings_revision"] == loaded["settings_revision"]


def test_secret_cas_is_serialized_across_store_instances(tmp_path, _restore_key):
    """Two app/store instances share the file lock: exactly one write can consume a revision."""
    clients = (TestClient(make_app(tmp_path)), TestClient(make_app(tmp_path)))
    revision = clients[0].get("/api/settings").json()["secret_revision"]

    def save(client, value):
        return client.put("/api/settings/secret", json={
            "key": "llm_api_key", "value": value, "expected_revision": revision,
        })

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(save, clients[0], "sk-process-one"),
            executor.submit(save, clients[1], "sk-process-two"),
        )
        responses = [future.result(timeout=10) for future in futures]

    assert sorted(response.status_code for response in responses) == [200, 409]
    accepted = next(response.json() for response in responses if response.status_code == 200)
    rejected = next(response.json()["detail"] for response in responses if response.status_code == 409)
    assert rejected["current_revision"] == accepted["secret_revision"]
    stored = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert stored["llm_api_key"] in {"sk-process-one", "sk-process-two"}
    assert stored[_REVISION_KEY] == accepted["secret_revision"]


def test_stored_secret_primes_env_on_app_start(tmp_path, _restore_key, monkeypatch):
    # A secret saved in a prior session is loaded into the env when the server next starts.
    # cwd = tmp_path (no local .env) so prime_env has nothing to defer to and primes the store.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secrets.json").write_text(json.dumps({"llm_api_key": "sk-from-disk"}), encoding="utf-8")
    os.environ.pop("LOOPLAB_LLM_API_KEY", None)
    TestClient(make_app(tmp_path))
    assert os.environ.get("LOOPLAB_LLM_API_KEY") == "sk-from-disk"


def test_dotenv_key_wins_over_stored_secret(tmp_path, _restore_key, monkeypatch):
    # The fix: a key the operator put in a local .env must WIN over the saved UI secret store — priming
    # must NOT clobber it (os.environ would otherwise outrank the .env file in pydantic).
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("LOOPLAB_LLM_API_KEY=sk-from-dotenv\n", encoding="utf-8")
    (tmp_path / "secrets.json").write_text(json.dumps({"llm_api_key": "sk-from-store"}), encoding="utf-8")
    os.environ.pop("LOOPLAB_LLM_API_KEY", None)
    TestClient(make_app(tmp_path))
    # os.environ is NOT primed from the store (so pydantic reads sk-from-dotenv from .env, not the store)
    assert os.environ.get("LOOPLAB_LLM_API_KEY") != "sk-from-store"
