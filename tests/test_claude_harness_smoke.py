"""CI verifies the smoke observer cannot call a refused tool a successful probe.

The installed Claude executable is exercised separately by the opt-in benchmark.
These tests drive the local Messages endpoint and the receipt/permission boundary,
without a provider account, executable install or scoring fixture.
"""
import json
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")

from benchmarks._claude_tool_fixture import ToolFixture, local_provider, tool_result
from benchmarks.claude_harness_smoke import run_client


def one_read():
    value = tool_result((yield "connection_check", {"run_id": "demo"}))
    assert value["ok"]


def message(result=None):
    return {"model": "local-fixture", "tools": [{"name": "mcp__looplab__connection_check"}],
            "messages": [] if result is None else [{"role": "user", "content": [result]}]}


def result_block(**overrides):
    return {"type": "tool_result", "tool_use_id": "tool_local_1",
            "content": [{"type": "text", "text": '{"ok":true}'}], **overrides}


@pytest.mark.parametrize("block", [result_block(is_error=True), result_block(content="not JSON"),
    result_block(content=[1]), result_block(content=[]),
    result_block(content=[{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}])])
def test_tool_result_rejects_client_failure_or_ambiguous_evidence(block):
    with pytest.raises((ValueError, RuntimeError)):
        tool_result(block)


def test_fixture_requires_the_exact_returned_tool_id_and_refuses_unexpected_requests():
    fixture = ToolFixture(one_read())
    events = fixture.message(message())
    assert events[1]["content_block"]["name"] == "mcp__looplab__connection_check"
    assert not fixture.done
    with pytest.raises(ValueError, match="expected MCP tool result"):
        fixture.message(message(result_block(tool_use_id="another-call")))
    fixture.message(message(result_block()))
    assert fixture.done and len(fixture.results) == 1
    with pytest.raises(ValueError, match="after fixture completion"):
        fixture.message(message())


def test_fixture_uses_actual_tool_result_to_complete_through_loopback_http():
    with local_provider(one_read()) as (url, fixture), httpx.Client(trust_env=False) as client:
        response = client.post(url + "/v1/messages", json=message())
        assert response.status_code == 200 and response.headers["content-type"] == "text/event-stream"
        assert not fixture.done
        response = client.post(url + "/v1/messages", json=message(result_block()))
        assert response.status_code == 200 and fixture.done and fixture.error is None


@pytest.mark.parametrize("denied", [True, False])
def test_client_exit_zero_does_not_hide_tool_refusal_and_keeps_environment_scoped(tmp_path, monkeypatch, denied):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-never-forwarded")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "account-never-forwarded")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")

    def fake_cli(argv, **kwargs):
        env = kwargs["env"]
        assert "LOOPLAB_UI_TOKEN" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
        assert "CLAUDE_CODE_USE_BEDROCK" not in env
        assert env["LOOPLAB_HARNESS_TOKEN"] == "test-scoped-only"
        assert env["ANTHROPIC_API_KEY"] == "local-fixture-only"
        assert argv[argv.index("--permission-mode") + 1] == "default"
        assert "--dangerously-skip-permissions" not in argv
        assert argv[argv.index("--tools") + 1] == ""
        assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
        with httpx.Client(trust_env=False) as http:
            assert http.post(env["ANTHROPIC_BASE_URL"] + "/v1/messages", json=message()).status_code == 200
            block = result_block(is_error=True, content="Permission required") if denied else result_block()
            http.post(env["ANTHROPIC_BASE_URL"] + "/v1/messages", json=message(block))
        # Deliberately misleading CLI success: the observer MUST inspect tool evidence.
        return SimpleNamespace(returncode=0, stdout=json.dumps({"is_error": False, "permission_denials": []}), stderr="")

    monkeypatch.setattr("benchmarks.claude_harness_smoke.subprocess.run", fake_cli)
    if denied:
        with pytest.raises(RuntimeError, match="acceptance failed"):
            run_client("fake-client", tmp_path, tmp_path / "config.json", "test-scoped-only", one_read(), "probe")
    else:
        result = run_client("fake-client", tmp_path, tmp_path / "config.json", "test-scoped-only", one_read(), "probe")
        assert result["tool_attempts"] == 1 and result["tool_errors"] == 0 and result["permission_denials"] == 0
