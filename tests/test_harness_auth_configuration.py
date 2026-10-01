"""A scoped token cannot leave an operator UI advertising an unusable anonymous plane."""
import pytest
import os
import subprocess
import sys
from fastapi.testclient import TestClient

from looplab.core.errors import EnvironmentRefusal
from looplab.serve.server import make_app


def _private(monkeypatch):
    for key in ("LOOPLAB_UI_TOKEN", "LOOPLAB_HARNESS_TOKEN", "JUPYTERHUB_SERVICE_PREFIX",
                "JUPYTERHUB_USER", "JUPYTERHUB_API_TOKEN", "LOOPLAB_UI_ANONYMOUS"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("owner", [None, ""])
def test_harness_without_owner_refuses_start_with_actionable_nonsecret_error(tmp_path, monkeypatch, owner):
    _private(monkeypatch)
    if owner is not None:
        monkeypatch.setenv("LOOPLAB_UI_TOKEN", owner)
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "private-agent-secret")
    with pytest.raises(EnvironmentRefusal, match="LOOPLAB_UI_TOKEN") as error:
        make_app(tmp_path, bind_host="127.0.0.1")
    assert "distinct" in str(error.value) and "restart" in str(error.value)
    assert "private-agent-secret" not in str(error.value)


def test_private_anonymous_default_still_matches_auth_status(tmp_path, monkeypatch):
    _private(monkeypatch)
    with TestClient(make_app(tmp_path, bind_host="127.0.0.1")) as client:
        assert client.get("/api/auth/status").json() == {"required": False, "authenticated": True}
        assert client.get("/api/runs").status_code == 200


def test_separate_owner_and_agent_credentials_keep_unlock_and_scope(tmp_path, monkeypatch):
    _private(monkeypatch)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "private-owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "private-agent-secret")
    with TestClient(make_app(tmp_path, bind_host="127.0.0.1")) as client:
        assert client.get("/api/auth/status").json() == {"required": True, "authenticated": False}
        assert client.get("/api/runs").status_code == 401
        owner = {"X-LoopLab-Token": "private-owner-secret"}
        agent = {"X-LoopLab-Token": "private-agent-secret"}
        assert client.get("/api/auth/status", headers=owner).json()["authenticated"] is True
        assert client.post("/api/auth/verify", headers=owner).status_code == 200
        assert client.get("/api/runs", headers=owner).status_code == 200
        assert client.get("/api/auth/status", headers=agent).json()["authenticated"] is False
        assert client.post("/api/start", headers=agent, json={}).status_code == 403


def test_published_server_can_resolve_its_existing_minted_owner_policy(tmp_path, monkeypatch):
    _private(monkeypatch)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN_FILE", str(tmp_path / "private-owner-token"))
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "private-agent-secret")
    with TestClient(make_app(tmp_path / "runs", bind_host="0.0.0.0")) as client:
        assert client.get("/api/auth/status").json() == {"required": True, "authenticated": False}
        owner = {"X-LoopLab-Token": (tmp_path / "private-owner-token").read_text().strip()}
        assert owner["X-LoopLab-Token"] != "private-agent-secret"
        assert client.get("/api/runs", headers=owner).status_code == 200


def test_harness_also_refuses_explicit_anonymous_shared_origin(tmp_path, monkeypatch):
    _private(monkeypatch)
    monkeypatch.setenv("LOOPLAB_UI_ANONYMOUS", "true")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "private-agent-secret")
    with pytest.raises(EnvironmentRefusal, match="distinct LOOPLAB_UI_TOKEN"):
        make_app(tmp_path, bind_host="0.0.0.0")


def test_real_cli_reports_setup_refusal_without_traceback_or_credential(tmp_path, monkeypatch):
    _private(monkeypatch)
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "private-agent-secret")
    monkeypatch.setenv("LOOPLAB_UI_DIST", str(tmp_path / "private-dist"))
    result = subprocess.run([sys.executable, "-m", "looplab.cli", "ui", "--no-build",
                             "--run-root", str(tmp_path / "runs"), "--port", "0"],
                            env=dict(os.environ), capture_output=True, timeout=20)
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    assert result.returncode == 2
    assert "distinct LOOPLAB_UI_TOKEN" in output and "restart" in output
    assert "Traceback" not in output and "private-agent-secret" not in output
