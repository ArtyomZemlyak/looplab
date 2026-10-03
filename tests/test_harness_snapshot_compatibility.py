"""Bootstrap cannot turn unknown settings into permission or an empty obligation."""
import json

import httpx
from fastapi.testclient import TestClient
import pytest

from looplab.core.config import CONFIG_SNAPSHOT_SCHEMA, CONFIG_SNAPSHOT_SCHEMA_KEY, RETIRED_SETTINGS
from looplab.harness.mcp_server import HarnessAPI
from tests.test_external_progress import _run


@pytest.mark.parametrize("route", ["harness-contract", "harness-handoff", "harness-progress"])
@pytest.mark.parametrize("damage,code", [("invalid_json", "harness_config_unavailable"),
    ("non_object", "harness_config_unavailable"), ("missing", "harness_config_unavailable"),
    ("future_format", "harness_config_incompatible"), ("unknown_setting", "harness_config_incompatible")])
def test_bootstrap_refuses_uninterpretable_config_without_touching_sources(tmp_path, route, damage, code):
    from looplab.events.run_generation import run_generation_token
    rd, store, original_client = _run(tmp_path)
    path = rd / "config.snapshot.json"
    data = json.loads(path.read_bytes())
    if damage == "invalid_json":
        path.write_bytes(b"private-secret invalid json")
    elif damage == "non_object":
        path.write_bytes(b"[]")
    elif damage == "missing":
        path.unlink()
    else:
        data[CONFIG_SNAPSHOT_SCHEMA_KEY if damage == "future_format" else "private-secret-new-obligation"] = (
            CONFIG_SNAPSHOT_SCHEMA + 1 if damage == "future_format" else True)
        path.write_text(json.dumps(data), encoding="utf8")
    before = {p.name: p.read_bytes() for p in rd.iterdir() if p.is_file()}
    generation = run_generation_token(store.read_all())
    with TestClient(original_client.app, raise_server_exceptions=False) as client:
        response = client.get("/api/runs/demo/" + route, params={"expected_generation": generation})
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == code
        assert response.json()["detail"]["source"] == "config.snapshot.json"
        assert "private-secret" not in response.text
    assert before == {p.name: p.read_bytes() for p in rd.iterdir() if p.is_file()}


def test_diagnostic_config_stays_readable_but_typed_bootstrap_cannot_approve_unknown_settings(tmp_path, monkeypatch):
    from looplab.events.run_generation import run_generation_token
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "scoped-fixture-token")
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "operator-fixture-token")
    rd, store, original_client = _run(tmp_path)
    path = rd / "config.snapshot.json"
    original = path.read_bytes()
    data = json.loads(original)
    data["future_admission_rule"] = True
    path.write_text(json.dumps(data), encoding="utf8")
    generation = run_generation_token(store.read_all())
    before = store.path.read_bytes()
    with TestClient(original_client.app) as client:
        def bridge(request):
            response = client.request(request.method, str(request.url), content=request.content, headers=request.headers)
            return httpx.Response(response.status_code, content=response.content)
        api = HarnessAPI("http://testserver", "scoped-fixture-token", transport=httpx.MockTransport(bridge))
        try:
            # The ordinary config view is diagnostic, not an effective contract.
            assert client.get("/api/runs/demo/config", headers={"X-LoopLab-Token": "operator-fixture-token"}).status_code == 200
            progress = api.run_progress("demo", generation)
            assert progress["status"] == 503 and progress["body"]["detail"]["code"] == "harness_config_incompatible"
            connection = api.connection_check("demo", generation)
            assert connection["ok"] is False and connection["at"] == "handoff", connection
            assert store.path.read_bytes() == before and json.loads(path.read_bytes()) == data
            # Operator restoration of the supported snapshot requires a new read.
            # The read itself grants no start/resume/training and rewrites nothing.
            path.write_bytes(original)
            fresh = api.run_progress("demo", generation)
            assert fresh["status"] == 200 and not fresh.get("code"), fresh
            assert api.connection_check("demo", generation)["ok"] is True
            assert store.path.read_bytes() == before and path.read_bytes() == original
        finally:
            api.client.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_supported_legacy_and_retired_settings_keep_bootstrap_working(tmp_path, legacy):
    from looplab.events.run_generation import run_generation_token
    rd, store, client = _run(tmp_path)
    path = rd / "config.snapshot.json"
    data = json.loads(path.read_bytes())
    data.update({name: True for name in RETIRED_SETTINGS})
    if not legacy:
        data[CONFIG_SNAPSHOT_SCHEMA_KEY] = CONFIG_SNAPSHOT_SCHEMA
    path.write_text(json.dumps(data), encoding="utf8")
    before = {p.name: p.read_bytes() for p in rd.iterdir() if p.is_file()}
    generation = run_generation_token(store.read_all())
    for route in ("harness-contract", "harness-handoff", "harness-progress"):
        response = client.get("/api/runs/demo/" + route, params={"expected_generation": generation})
        assert response.status_code == 200, response.text
    assert before == {p.name: p.read_bytes() for p in rd.iterdir() if p.is_file()}
