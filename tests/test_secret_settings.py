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

from looplab.core.atomicio import atomic_write_text  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from looplab.serve.settings_store import SettingsStore, _REVISION_KEY  # noqa: E402


def _put_secret(client, value, *, settings_revision=None, secret_revision=None):
    """Save a credential with the two preconditions a nonempty write now REQUIRES.

    A credential is stored bound to the endpoint the caller reviewed, so saving one is a
    precondition write: both `expected_settings_revision` (the endpoint) and
    `expected_secret_revision` (the credential) must come from one Settings snapshot. Omitting
    either is a 428 before any CAS runs, so a test that omits them asserts the precondition error
    rather than the storage, masking, env-priming or CAS behaviour it was written for. Clearing
    needs no precondition — there is no endpoint to bind."""
    snapshot = client.get("/api/settings").json()
    body = {"key": "llm_api_key", "value": value}
    if value:
        body["expected_settings_revision"] = (
            snapshot["settings_revision"] if settings_revision is None else settings_revision)
        body["expected_secret_revision"] = (
            snapshot["secret_revision"] if secret_revision is None else secret_revision)
    return client.put("/api/settings/secret", json=body)



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

    r = _put_secret(client, "sk-secret-123")
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["key"] == "llm_api_key" and r.json()["set"] is True
    assert isinstance(r.json()["secret_revision"], str) and r.json()["secret_revision"]

    # persisted to the dedicated owner-only file, never ui_settings.json — as a PAIR: the key and
    # the endpoint it is bound to, so it can never be replayed against a different host.
    stored = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert stored == {
        "llm_api_key": "sk-secret-123",
        "llm_api_key_base_url": "http://localhost:11434/v1",
        _REVISION_KEY: r.json()["secret_revision"],
    }
    if (tmp_path / "ui_settings.json").exists():
        assert "llm_api_key" not in json.loads((tmp_path / "ui_settings.json").read_text(encoding="utf-8"))

    # NEVER installed process-globally. It used to be exported to os.environ so a child would
    # inherit it, which also handed it to every unrelated subprocess this server spawns — including
    # sandboxed candidate code. Children now receive it through the fenced per-spawn overlay
    # (`SettingsStore.launch_env_for_run`) instead, so assert the absence: a regression that
    # restores the export must fail here.
    assert "LOOPLAB_LLM_API_KEY" not in os.environ
    resolved = SettingsStore(tmp_path).resolve_settings()
    assert resolved.llm_api_key.get_secret_value() == "sk-secret-123"   # owner-side calls still see it

    # the API echoes the secret ONLY as the mask — never the value
    settings = client.get("/api/settings").json()["settings"]
    assert settings["llm_api_key"] == "***"
    assert "sk-secret-123" not in json.dumps(settings)


def test_secret_clear_and_reject_unknown(tmp_path, _restore_key):
    client = TestClient(make_app(tmp_path))
    _put_secret(client, "sk-zzz")
    assert SettingsStore(tmp_path).resolve_settings().llm_api_key.get_secret_value() == "sk-zzz"
    assert "LOOPLAB_LLM_API_KEY" not in os.environ      # owner-side only, never process-global

    # an empty value clears it from both the store and the env
    r = _put_secret(client, "")
    assert r.json()["set"] is False
    assert json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8")) == {
        _REVISION_KEY: r.json()["secret_revision"]}
    # Clearing drops the endpoint binding with the key: a bound pair must never outlive its key.
    assert "llm_api_key_base_url" not in json.loads(
        (tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert "LOOPLAB_LLM_API_KEY" not in os.environ
    assert SettingsStore(tmp_path).resolve_settings().llm_api_key is None
    assert client.get("/api/settings").json()["settings"]["llm_api_key"] is None

    # an unknown secret key is rejected
    assert client.put("/api/settings/secret", json={"key": "nope", "value": "x"}).status_code == 400


def test_secret_cas_rejects_old_delayed_write_without_leaking_values(tmp_path, _restore_key):
    client = TestClient(make_app(tmp_path))
    loaded = client.get("/api/settings").json()
    old_revision = loaded["secret_revision"]

    newer = _put_secret(client, "sk-newer-value", secret_revision=old_revision)
    assert newer.status_code == 200
    new_revision = newer.json()["secret_revision"]
    assert new_revision != old_revision

    delayed = _put_secret(client, "sk-old-delayed", secret_revision=old_revision)
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
        return _put_secret(client, value, secret_revision=revision)

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


def test_server_start_reads_a_stored_secret_without_exporting_it(tmp_path, _restore_key, monkeypatch):
    """A credential saved in a prior session is usable again — and still never process-global.

    This used to assert that startup PRIMED `os.environ`. That export is gone on purpose: it handed
    the owner's key to every subprocess the server spawns, sandboxed candidate code included.
    Startup now only makes the stored pair resolvable to owner-side calls, and children receive it
    through the fenced per-spawn overlay instead.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secrets.json").write_text(json.dumps({
        "llm_api_key": "sk-from-disk",
        "llm_api_key_base_url": "http://localhost:11434/v1",
    }), encoding="utf-8")
    os.environ.pop("LOOPLAB_LLM_API_KEY", None)
    TestClient(make_app(tmp_path))
    assert "LOOPLAB_LLM_API_KEY" not in os.environ
    resolved = SettingsStore(tmp_path).resolve_settings()
    assert resolved.llm_api_key.get_secret_value() == "sk-from-disk"


def test_worker_refreshes_rotated_and_revoked_stored_secret(tmp_path, _restore_key, monkeypatch):
    """A sibling process's atomic file update invalidates this worker before its next paid action.

    The refresh returns a PER-SPAWN overlay now instead of mutating `os.environ`, so rotation and
    revocation are observed in what the next child would be handed. Revocation is an explicit empty
    tombstone, not an omission: a child with its own `.env` must not fall back to a key the owner
    just revoked.
    """
    monkeypatch.chdir(tmp_path)
    secret_path = tmp_path / "secrets.json"
    endpoint = "http://localhost:11434/v1"
    rd = tmp_path / "worker-run"
    rd.mkdir()
    (rd / "config.snapshot.json").write_text(
        json.dumps({"llm_base_url": endpoint}), encoding="utf-8")
    atomic_write_text(secret_path, json.dumps({
        "llm_api_key": "sk-worker-old", "llm_api_key_base_url": endpoint,
        _REVISION_KEY: "revision-old",
    }))
    os.environ.pop("LOOPLAB_LLM_API_KEY", None)
    worker = SettingsStore(tmp_path)
    assert worker.refresh_env_secrets(rd)["LOOPLAB_LLM_API_KEY"] == "sk-worker-old"

    atomic_write_text(secret_path, json.dumps({
        "llm_api_key": "sk-worker-new", "llm_api_key_base_url": endpoint,
        _REVISION_KEY: "revision-new",
    }))
    assert worker.refresh_env_secrets(rd)["LOOPLAB_LLM_API_KEY"] == "sk-worker-new"

    atomic_write_text(secret_path, json.dumps({_REVISION_KEY: "revision-cleared"}))
    assert worker.refresh_env_secrets(rd)["LOOPLAB_LLM_API_KEY"] == ""
    assert "LOOPLAB_LLM_API_KEY" not in os.environ    # never process-global, at any point


def test_worker_refresh_preserves_operator_owned_environment(
        tmp_path, _restore_key, monkeypatch):
    """Revision invalidation owns only values the store injected, never a later explicit override."""
    monkeypatch.chdir(tmp_path)
    secret_path = tmp_path / "secrets.json"
    atomic_write_text(secret_path, json.dumps({
        "llm_api_key": "sk-worker-old", _REVISION_KEY: "revision-old",
    }))
    os.environ.pop("LOOPLAB_LLM_API_KEY", None)
    worker = SettingsStore(tmp_path)
    worker.prime_env()
    os.environ["LOOPLAB_LLM_API_KEY"] = "sk-operator-override"

    atomic_write_text(secret_path, json.dumps({
        "llm_api_key": "sk-worker-new", _REVISION_KEY: "revision-new",
    }))
    worker.refresh_env_secrets()
    assert os.environ["LOOPLAB_LLM_API_KEY"] == "sk-operator-override"


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


def test_stored_secret_reaches_disk_before_the_rename_publishes_it(tmp_path, _restore_key, monkeypatch):
    """The write is a temp + os.replace, so the rename is only atomic if the DATA is already durable.
    Without the sync a crash can land the rename while the blocks are lost, publishing an empty
    secrets.json — the credential silently gone and the CAS revision reset to its initial token."""
    from looplab.serve import settings_store as store_module

    order: list[str] = []
    real_sync, real_replace = store_module.best_effort_fsync, os.replace

    def watched_sync(fileno):
        order.append("sync")
        return real_sync(fileno)

    def watched_replace(src, dst, *args, **kwargs):
        if str(dst).endswith("secrets.json"):
            order.append("replace")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(store_module, "best_effort_fsync", watched_sync)
    monkeypatch.setattr(store_module.os, "replace", watched_replace)

    store = SettingsStore(tmp_path)
    # A nonempty credential is stored only as a PAIR, so the binding is part of the durable write
    # this test times: without it the store refuses before any temp file is created.
    store.store_secret("llm_api_key", "sk-durable", binding="http://localhost:11434/v1")

    assert order == ["sync", "replace"], order
    assert store.load_secrets()["llm_api_key"] == "sk-durable"
