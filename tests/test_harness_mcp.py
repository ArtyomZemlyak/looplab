"""The MCP transport must go through the server's existing authorization and receipts."""
import httpx
import pytest
import anyio

from looplab.harness.mcp_server import HarnessAPI, build_server


def test_mcp_advertises_run_controls_and_full_settings_discovery():
    pytest.importorskip("mcp")
    api = HarnessAPI("http://127.0.0.1:8765", transport=httpx.MockTransport(
        lambda request: pytest.fail("listing local tools contacted the UI")))
    tools = anyio.run(build_server(api).list_tools)
    assert {"capabilities", "phases", "phase_info", "settings_keys", "setting_info", "operations",
            "operation_schema", "api_request"} == {tool.name for tool in tools}


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
