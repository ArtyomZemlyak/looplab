"""A received HTTP response can still fail to acknowledge an already accepted write."""
import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI, MAX_RESPONSE_BYTES


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("fault", ["oversized", "invalid_json", "server_error"])
def test_incomplete_write_acknowledgement_preserves_status_without_inviting_new_write(method, fault):
    seen = []
    def handler(request):
        seen.append(request)
        if fault == "oversized":
            return httpx.Response(200, json={"padding": "x" * MAX_RESPONSE_BYTES})
        if fault == "invalid_json":
            return httpx.Response(200, text="<html>private-login-page</html>")
        return httpx.Response(503, json={"detail": "acknowledgement unavailable"})
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    result = api.request(method, "/api/runs/demo/commands", {}, "original-key")
    assert result["status"] == (503 if fault == "server_error" else 200)
    assert result["code"] == "request_outcome_unknown" and result["outcome"] == "unknown"
    assert "original" in result["message"] and "narrower" not in result["message"]
    assert "private-login-page" not in str(result)
    assert len(seen) == 1 and seen[0].headers["Idempotency-Key"] == "original-key"
    if fault == "server_error":
        assert result["body"] == {"detail": "acknowledgement unavailable"}
    if fault == "oversized":
        assert result["truncated"] and result["bytes"] > MAX_RESPONSE_BYTES and "body" not in result


@pytest.mark.parametrize("tool", ["run_progress", "command_receipt"])
@pytest.mark.parametrize("body", [b"<html>private-login-page</html>", b"null", b"[]"])
def test_typed_read_refuses_success_shaped_nonobject_evidence(tool, body):
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body)))
    result = (api.run_progress("demo", "a" * 64) if tool == "run_progress" else
              api.command_receipt("demo", "a" * 64, idempotency_key="original-key"))
    assert result["status"] == 200 and result["outcome"] == "unavailable"
    assert result["code"] == "response_incomplete" and "body" not in result
    assert "private-login-page" not in str(result)


def test_oversized_read_keeps_narrow_query_advice_and_no_partial_body():
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"padding": "x" * MAX_RESPONSE_BYTES})))
    result = api.request("GET", "/api/runs/demo/events")
    assert result["status"] == 200 and result["outcome"] == "unavailable"
    assert result["code"] == "response_incomplete" and "narrower" in result["message"]
    assert result["truncated"] and "body" not in result


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422])
def test_validation_refusals_keep_original_status_and_body(status):
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={"detail": "input refused"})))
    assert api.request("POST", "/api/runs/demo/commands", {}) == {
        "status": status, "body": {"detail": "input refused"}}


def test_generic_text_reads_and_empty_no_content_writes_remain_supported():
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(204) if request.method == "DELETE" else httpx.Response(200, text="source code")))
    assert api.request("GET", "/api/runs/demo/artifact") == {"status": 200, "body": "source code"}
    assert api.request("DELETE", "/api/runs/demo/resource") == {"status": 204, "body": ""}
