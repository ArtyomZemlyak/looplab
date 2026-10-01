"""Typed completion reads fence context and keep paging a single explicit GET."""
import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI, MAX_RESPONSE_BYTES

GEN = "a" * 64


def test_result_page_encodes_literal_identity_and_opaque_cursor_once():
    seen = []
    page = {"generation": GEN.upper(), "items": [], "next_cursor": None, "has_more": False}
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=page)
    api = HarnessAPI("http://localhost", "scoped", transport=httpx.MockTransport(handler))
    assert api.result_notices("demo # %2F", GEN, limit=1, cursor="opaque?&+%#") == {"status": 200, "body": page}
    request, = seen
    assert request.method == "GET" and request.content == b""
    assert request.url.path == "/api/runs/demo # %2F/result-notices"
    assert dict(request.url.params) == {"expected_generation": GEN, "limit": "1", "cursor": "opaque?&+%#"}
    assert request.headers["X-LoopLab-Token"] == "scoped"
    assert "Idempotency-Key" not in request.headers


@pytest.mark.parametrize("generation", [None, "bad", "b" * 64])
def test_result_page_refuses_unbound_generation_without_echoing_receipts(generation):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"generation": generation, "items": ["private-wrong-context"]})
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    value = api.result_notices("demo", GEN)
    assert value["status"] == 200 and value["outcome"] == "unavailable"
    assert value["code"] == ("response_context_mismatch" if generation == "b" * 64 else "response_incomplete")
    assert "body" not in value and "private-wrong-context" not in str(value)
    assert len(seen) == 1


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 201}, {"limit": True}, {"limit": 1.5},
    {"cursor": ""}, {"cursor": "x" * 257}, {"cursor": 1}, {"run_id": "../demo"}, {"expected_generation": "bad"}])
def test_bad_page_arguments_refuse_before_transport(args):
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: pytest.fail("invalid read contacted server")))
    with pytest.raises(ValueError):
        api.result_notices(**{"run_id": "demo", "expected_generation": GEN, **args})


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 503])
def test_http_refusal_is_preserved_with_no_retry(status):
    seen = []
    detail = {"detail": {"code": "result_notice_cursor_changed", "remediation": "refresh"}}
    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=detail)
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    assert api.result_notices("demo", GEN) == {"status": status, "body": detail}
    assert len(seen) == 1


@pytest.mark.parametrize("fault", ["disconnect", "html", "list", "oversized"])
def test_lost_or_incomplete_page_is_unavailable_not_empty_results(fault):
    seen = []
    def handler(request):
        seen.append(request)
        if fault == "disconnect":
            raise httpx.ReadError("private network URL", request=request)
        if fault == "list":
            return httpx.Response(200, json=[])
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1) if fault == "oversized" else b"<html>login</html>")
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    value = api.result_notices("demo", GEN)
    assert value["outcome"] == "unavailable" and "body" not in value
    assert value["status"] == (None if fault == "disconnect" else 200)
    assert "private network URL" not in str(value) and len(seen) == 1
