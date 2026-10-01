"""Unavailable live discovery must not be mistaken for an empty capability catalog."""
import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI, MAX_RESPONSE_BYTES


def call(api, tool):
    return api.operations("commands") if tool == "operations" else api.schema("/api/runs/{run_id}/commands")


@pytest.mark.parametrize("tool", ["operations", "schema"])
@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_discovery_transport_loss_returns_fixed_unavailable_without_retry(tool, failure):
    seen = []
    def handler(request):
        seen.append(request)
        raise failure("private-url-token-details", request=request)
    api = HarnessAPI("http://localhost/proxy", "scoped-secret", transport=httpx.MockTransport(handler))
    value = call(api, tool)
    assert value["status"] is None and value["code"] == "api_unreachable"
    assert value["outcome"] == "unavailable" and value["at"] == "openapi"
    assert "private-url-token-details" not in str(value) and "scoped-secret" not in str(value)
    assert len(seen) == 1 and seen[0].method == "GET" and seen[0].url.path == "/proxy/openapi.json"
    assert seen[0].headers["X-LoopLab-Token"] == "scoped-secret"


@pytest.mark.parametrize("tool", ["operations", "schema"])
@pytest.mark.parametrize("status", [302, 401, 403, 404, 503])
def test_discovery_http_refusal_preserves_status_without_raw_body(tool, status):
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json={"detail": "private-error"})))
    value = call(api, tool)
    assert value["status"] == status and value["outcome"] == "unavailable"
    assert value["code"] == "api_read_failed" and value["at"] == "openapi"
    assert "body" not in value and "private-error" not in str(value)


@pytest.mark.parametrize("tool", ["operations", "schema"])
@pytest.mark.parametrize("payload", [None, [], {}, {"paths": []}, {"paths": {"/api/x": []}},
    {"paths": {"/api/x": {"get": None}}}, {"paths": {}, "components": []},
    {"paths": {}, "components": {"schemas": []}}])
def test_malformed_catalog_is_unavailable_not_an_empty_capability_list(tool, payload):
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    value = call(api, tool)
    assert value["status"] == 200 and value["code"] == "response_incomplete"
    assert value["outcome"] == "unavailable" and value["reason"] == "invalid_response"
    assert "matches" not in value and "operations" not in value and "body" not in value


@pytest.mark.parametrize("tool", ["operations", "schema"])
def test_non_json_discovery_is_unavailable(tool):
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="private-login-page")))
    value = call(api, tool)
    assert value["status"] == 200 and value["code"] == "response_incomplete"
    assert "private-login-page" not in str(value)


def test_valid_empty_live_catalog_remains_distinct_from_a_failed_read():
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"paths": {}})))
    assert api.operations() == {"matches": [], "total": 0}


def test_discovery_does_not_contain_programming_errors():
    def handler(request):
        raise RuntimeError("programming defect")
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="programming defect"):
        api.operations()


def test_full_catalog_stays_readable_and_selected_recursive_schemas_are_preserved():
    catalog = {"paths": {"/api/runs/{run_id}/commands": {"parameters": [], "x-extra": [],
        "post": {"summary": "Submit command", "description": "x" * MAX_RESPONSE_BYTES,
                 "requestBody": {"$ref": "#/components/schemas/Command"}}}},
        "components": {"schemas": {
            "Command": {"properties": {"idea": {"$ref": "#/components/schemas/Idea"}}},
            "Idea": {"properties": {"parent": {"$ref": "#/components/schemas/Command"}}},
            "Unrelated": {"type": "string"}}}}
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=catalog)))
    assert api.operations("commands")["total"] == 1
    selected = api.schema("/api/runs/{run_id}/commands")
    assert selected["operations"] == catalog["paths"][selected["path"]]
    assert set(selected["components"]) == {"Command", "Idea"}
