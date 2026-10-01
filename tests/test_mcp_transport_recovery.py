"""A network failure is not a failed durable write or permission to submit again."""
import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout,
                                   httpx.WriteError, httpx.RemoteProtocolError])
def test_transport_loss_returns_fixed_unknown_write_or_unavailable_read_without_retry(method, error):
    seen = []
    def handler(request):
        seen.append(request)
        raise error("private-token-and-sensitive-url", request=request)
    api = HarnessAPI("http://localhost", "private-token", transport=httpx.MockTransport(handler))
    result = api.request(method, "/api/runs/demo/commands", {"private": "payload"}, "original-key")
    assert result["status"] is None
    assert result["outcome"] == ("unavailable" if method == "GET" else "unknown")
    assert result["code"] == ("api_unreachable" if method == "GET" else "request_outcome_unknown")
    assert "original" in result["message"] if method != "GET" else "read" in result["message"]
    assert len(seen) == 1
    assert not any(secret in str(result) for secret in ("private-token", "sensitive-url", "payload", "original-key"))


def test_receipt_read_failure_cannot_report_absence_or_restart_worker():
    seen = []
    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout("sensitive", request=request)
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    result = api.command_receipt("demo", "a" * 64, idempotency_key="original-key")
    assert result["outcome"] == "unavailable" and result["status"] is None
    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].url.path.endswith("/command-receipt")


def test_transport_containment_does_not_hide_programming_errors():
    def handler(request):
        raise RuntimeError("implementation bug")
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="implementation bug"):
        api.request("POST", "/api/runs/demo/commands", {})
