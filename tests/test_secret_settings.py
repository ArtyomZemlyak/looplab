"""Secure UI secret store (the LLM API key). PUT /api/settings/secret writes an owner-only
secrets.json (never ui_settings.json / a run snapshot), applies the value to the process env so a
spawned engine inherits it, and the API only ever echoes the masked "***". Skipped without [ui]."""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.server import make_app  # noqa: E402


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
    assert r.json() == {"ok": True, "key": "llm_api_key", "set": True}

    # persisted to the dedicated owner-only file, never ui_settings.json
    assert json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8")) == {"llm_api_key": "sk-secret-123"}
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
    assert json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8")) == {}
    assert "LOOPLAB_LLM_API_KEY" not in os.environ
    assert client.get("/api/settings").json()["settings"]["llm_api_key"] is None

    # an unknown secret key is rejected
    assert client.put("/api/settings/secret", json={"key": "nope", "value": "x"}).status_code == 400


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
