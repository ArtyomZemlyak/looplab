"""The MCP transport must go through the server's existing authorization and receipts."""
import httpx
import pytest
import anyio

from looplab.harness.mcp_server import HarnessAPI, MCP_INSTRUCTIONS, build_server, run_stdio


def test_mcp_advertises_run_controls_and_full_settings_discovery():
    pytest.importorskip("mcp")
    api = HarnessAPI("http://127.0.0.1:8765", transport=httpx.MockTransport(
        lambda request: pytest.fail("listing local tools contacted the UI")))
    tools = anyio.run(build_server(api).list_tools)
    assert {"capabilities", "phases", "phase_info", "settings_keys", "setting_info", "operations",
            "operation_schema", "api_request", "run_progress", "result_notices", "command_receipt", "connection_check",
            "upstream_status", "upstream_request", "upstream_propose", "upstream_check", "upstream_advance"} == {tool.name for tool in tools}
    for tool in (t for t in tools if t.name.startswith("upstream_")):
        hints = tool.annotations.model_dump(by_alias=True)
        assert hints["idempotentHint"]
        assert hints["readOnlyHint"] == (tool.name in ("upstream_status", "upstream_request"))
        assert hints["destructiveHint"] == (tool.name not in ("upstream_status", "upstream_request"))
    connection = next(tool for tool in tools if tool.name == "connection_check")
    hints = connection.annotations.model_dump(by_alias=True)
    assert hints["readOnlyHint"] and hints["idempotentHint"] and not hints["destructiveHint"]
    progress = next(tool for tool in tools if tool.name == "run_progress")
    hints = progress.annotations.model_dump(by_alias=True)
    assert hints["readOnlyHint"] and hints["idempotentHint"]
    assert not hints["destructiveHint"]
    progress_schema = progress.model_dump(by_alias=True)["inputSchema"]
    assert progress_schema["required"] == ["run_id", "expected_generation"]
    assert progress_schema["properties"]["language"]["enum"] == ["en", "ru"]
    assert progress_schema["properties"]["language"]["default"] == "en"
    receipt = next(tool for tool in tools if tool.name == "command_receipt")
    hints = receipt.annotations.model_dump(by_alias=True)
    assert hints["readOnlyHint"] and not hints["destructiveHint"]
    results = next(tool for tool in tools if tool.name == "result_notices")
    hints = results.annotations.model_dump(by_alias=True)
    assert hints["readOnlyHint"] and hints["idempotentHint"] and not hints["destructiveHint"]
    schema = results.model_dump(by_alias=True)["inputSchema"]
    assert schema["required"] == ["run_id", "expected_generation"]
    assert schema["properties"]["limit"]["default"] == 50
    assert build_server(api).instructions == MCP_INSTRUCTIONS
    assert "current generation" in MCP_INSTRUCTIONS[:512]
    assert "command_receipt" in MCP_INSTRUCTIONS[:512]


@pytest.mark.parametrize("scoped,owner", [(None, None), (None, "owner-secret"),
    ("", "owner-secret"), ("   ", "owner-secret"), ("${LOOPLAB_HARNESS_TOKEN}", None),
    ("same-secret", "same-secret"), ("secret\nvalue", None),
    ("secret\x7fvalue", None), ("secret\u0100value", None)])
def test_stdio_never_uses_an_owner_fallback_or_opens_transport_without_scoped_token(monkeypatch, scoped, owner):
    import looplab.harness.mcp_server as module
    for name, value in [("LOOPLAB_HARNESS_TOKEN", scoped), ("LOOPLAB_UI_TOKEN", owner)]:
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(module, "HarnessAPI", lambda *_a, **_kw: pytest.fail("invalid setup opened transport"))
    with pytest.raises(ValueError) as exc:
        run_stdio()
    assert "LOOPLAB_HARNESS_TOKEN" in str(exc.value) or "LOOPLAB_UI_TOKEN" in str(exc.value)
    assert "owner-secret" not in str(exc.value) and "same-secret" not in str(exc.value)
    if scoped and scoped.strip():
        assert scoped not in str(exc.value)


@pytest.mark.parametrize("token", ["", "secret\x00value"])
def test_invalid_explicit_token_does_not_fall_back_to_valid_environment(monkeypatch, token):
    import looplab.harness.mcp_server as module
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "valid-scoped-secret")
    monkeypatch.setattr(module, "HarnessAPI", lambda *_a, **_kw: pytest.fail("invalid token opened transport"))
    with pytest.raises(ValueError) as exc:
        run_stdio(token=token)
    assert "valid-scoped-secret" not in str(exc.value)
    assert "secret\x00value" not in str(exc.value)


@pytest.mark.parametrize("explicit", [False, True])
def test_stdio_forwards_only_selected_scoped_credential_and_closes_transport(monkeypatch, explicit):
    import looplab.harness.mcp_server as module
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "scoped-secret")
    clients = []
    def api(url, token):
        assert url == "http://127.0.0.1:8765"
        assert token == ("explicit-secret" if explicit else "scoped-secret")
        value = HarnessAPI(url, token, transport=httpx.MockTransport(lambda _: pytest.fail("unexpected HTTP")))
        clients.append(value)
        return value
    class Server:
        def run(self, transport):
            assert transport == "stdio"
    monkeypatch.setattr(module, "HarnessAPI", api)
    monkeypatch.setattr(module, "build_server", lambda _: Server())
    run_stdio(url="http://127.0.0.1:8765", token="explicit-secret" if explicit else None)
    assert clients[0].client.is_closed


def test_cli_reports_missing_credential_before_opening_stdio(monkeypatch):
    from typer.testing import CliRunner
    from looplab.cli import app
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    result = CliRunner().invoke(app, ["harness-mcp"])
    assert result.exit_code == 2
    assert "LOOPLAB_HARNESS_TOKEN" in result.output
    assert "owner-secret" not in result.output


@pytest.mark.parametrize("status", [200, 404, 409, 503])
@pytest.mark.parametrize("by_key", [True, False])
def test_command_receipt_is_one_get_with_original_key_in_header_only(status, by_key):
    seen = []
    from looplab.serve.command_identity import command_identity
    payload = ({"version": 1, "generation": "b" * 64, "command": {"id": command_identity("original-key")[0]
        if by_key else "cmd_" + "a" * 32, "status": "succeeded", "event_type": "inject_node",
        "event_seq": 3, "error_code": "", "retryable": False}, "terminal": True}
        if status == 200 else {"unchanged": True})
    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=payload)
    api = HarnessAPI("http://127.0.0.1:8765/proxy", "scoped", transport=httpx.MockTransport(handler))
    args = {"idempotency_key": "original-key"} if by_key else {"command_id": "cmd_" + "a" * 32}
    assert api.command_receipt("demo # %2F", "b" * 64, **args) == {"status": status, "body": payload}
    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].url.path == "/proxy/api/runs/demo # %2F/command-receipt"
    assert seen[0].url.params["expected_generation"] == "b" * 64
    assert "original-key" not in str(seen[0].url)
    assert seen[0].headers.get("Idempotency-Key", "") == ("original-key" if by_key else "")
    assert not seen[0].content


@pytest.mark.parametrize("args", [{}, {"command_id": "bad"},
    {"command_id": "cmd_" + "a" * 32, "idempotency_key": "same"},
    {"idempotency_key": "key\nheader"}, {"idempotency_key": "x" * 513}])
def test_receipt_invalid_lookup_never_reaches_transport(args):
    api = HarnessAPI("http://127.0.0.1:8765", transport=httpx.MockTransport(
        lambda request: pytest.fail("invalid receipt lookup reached transport")))
    with pytest.raises(ValueError):
        api.command_receipt("demo", "a" * 64, **args)


def test_discovery_and_command_forwarding_keep_auth_and_idempotency():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json={"paths": {"/api/runs/{run_id}/commands": {
                "post": {"summary": "Durable command", "requestBody": {
                    "$ref": "#/components/schemas/Command"}}}},
                "components": {"schemas": {"Command": {"type": "object"},
                                            "Unrelated": {"type": "string"}}}})
        return httpx.Response(200, json={"status": "accepted"})

    api = HarnessAPI("http://127.0.0.1:8765", "token-value",
                     transport=httpx.MockTransport(handler))
    assert api.operations("command")["total"] == 1
    spec = api.schema("/api/runs/{run_id}/commands")
    assert set(spec["components"]) == {"Command"}
    result = api.request("POST", "/api/runs/demo/commands",
                         {"type": "inject_node", "data": {}, "expected_generation": "a" * 64},
                         "agent:demo:turn-1")
    assert result == {"status": 200, "body": {"status": "accepted"}}
    assert seen[-1].headers["X-LoopLab-Token"] == "token-value"
    assert seen[-1].headers["Idempotency-Key"] == "agent:demo:turn-1"


@pytest.mark.parametrize("path", ["https://attacker.example/api/x", "//attacker.example/api/x",
                                "/api/../secret", "/api/%2e%2e/secret", "/api/x#fragment",
                                "/api/x%2f%2fhost", "/health"])
def test_api_request_refuses_paths_outside_control_plane(path):
    api = HarnessAPI("http://127.0.0.1:8765", transport=httpx.MockTransport(
        lambda request: pytest.fail("an invalid path reached the transport")))
    with pytest.raises(ValueError):
        api.request("GET", path)


def test_api_refuses_redirect_to_avoid_forwarding_owner_token():
    api = HarnessAPI("http://127.0.0.1:8765", "secret", transport=httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"Location": "https://example.com/"})))
    assert api.request("GET", "/api/runs")["status"] == 302


@pytest.mark.parametrize("status", [200, 403, 409, 503])
@pytest.mark.parametrize("run_id", ["demo space", "mnist # %2F"])
def test_compact_progress_is_one_fenced_authenticated_read(status, run_id):
    seen = []
    from tests.test_harness_connection import progress
    payload = {**progress(), "receipt": "unchanged"} if status == 200 else {"receipt": "unchanged"}

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=payload)

    api = HarnessAPI("http://127.0.0.1:8765", "scoped-token",
                     transport=httpx.MockTransport(handler))
    assert api.run_progress(run_id, "a" * 64) == {
        "status": status, "body": payload}
    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].url.path == f"/api/runs/{run_id}/harness-progress"
    assert dict(seen[0].url.params) == {"expected_generation": "a" * 64, "brief": "true"}
    assert seen[0].headers["X-LoopLab-Token"] == "scoped-token"
    assert "Idempotency-Key" not in seen[0].headers


@pytest.mark.parametrize("run_id,generation", [
    ("..", "a" * 64), ("x/y", "a" * 64), ("x\\y", "a" * 64),
    ("/absolute", "a" * 64), ("", "a" * 64), ("demo", "stale")])
def test_compact_progress_refuses_invalid_identity_before_transport(run_id, generation):
    api = HarnessAPI("http://127.0.0.1:8765", transport=httpx.MockTransport(
        lambda request: pytest.fail("invalid identity reached the transport")))
    with pytest.raises(ValueError):
        api.run_progress(run_id, generation)
