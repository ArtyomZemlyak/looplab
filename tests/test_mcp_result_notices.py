"""Typed completion reads fence context and keep paging a single explicit GET."""
import httpx
import json
import pytest

from looplab.harness.mcp_server import HarnessAPI, MAX_RESPONSE_BYTES

GEN = "a" * 64


def test_result_page_encodes_literal_identity_and_opaque_cursor_once():
    seen = []
    page = {"version": 1, "generation": GEN.upper(), "total": 0,
            "items": [], "next_cursor": None, "has_more": False}
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


def _page():
    return {"version": 1, "generation": GEN, "total": 1, "has_more": False, "next_cursor": None,
        "items": [{"id": "node:0:0", "kind": "node", "node_id": 0, "attempt": 0,
                   "status": "evaluated", "score": .25, "confirmed_mean": None,
                   "evidence_token": "b" * 64}]}


@pytest.mark.parametrize("field", ["version", "total", "items", "has_more", "next_cursor"])
def test_page_requires_completion_and_pagination_fields(field):
    page = _page()
    del page[field]
    _refused_page(page)


def _refused_page(page):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=page)
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    result = api.result_notices("demo", GEN)
    assert result["status"] == 200 and result["code"] == "response_incomplete"
    assert result["outcome"] == "unavailable" and result["reason"] == "invalid_result_page"
    assert "body" not in result and len(seen) == 1


@pytest.mark.parametrize("changes", [
    {"version": True}, {"version": 2}, {"total": True}, {"total": -1}, {"total": 0},
    {"items": {}}, {"items": [None]}, {"has_more": 0},
    {"has_more": True}, {"next_cursor": "opaque"},
    {"has_more": True, "next_cursor": ""},
    {"has_more": True, "next_cursor": "x" * 257},
    {"has_more": True, "next_cursor": 1},
    {"total": 2}, {"total": 0, "items": [], "has_more": True, "next_cursor": "opaque"},
])
def test_page_refuses_invalid_or_inconsistent_envelope(changes):
    _refused_page({**_page(), **changes})


@pytest.mark.parametrize("changes", [
    {"id": "run"}, {"kind": "run"}, {"node_id": True}, {"node_id": 1},
    {"attempt": -1}, {"attempt": True}, {"attempt": 1},
    {"status": "pending"}, {"evidence_token": "bad"},
    {"score": True}, {"score": "0.25"}, {"confirmed_mean": False},
    {"status": "failed"}, {"status": "aborted"}, {"score": None, "confirmed_mean": "bad"},
])
def test_page_refuses_unbound_or_nonterminal_receipts(changes):
    page = _page()
    page["items"][0].update(changes)
    _refused_page(page)


def test_page_refuses_duplicate_receipts():
    page = _page()
    page.update(total=2, items=page["items"] * 2)
    _refused_page(page)


@pytest.mark.parametrize("status", ["evaluated", "failed", "aborted", "finished"])
def test_valid_terminal_pages_preserve_evidence_and_extra_fields(status):
    page = _page()
    row = page["items"][0]
    row.update(status=status, future_metadata={"retained": True})
    if status in {"failed", "aborted"}:
        row["score"] = None
    if status == "finished":
        row.update(id="run", kind="run", attempt=None, selected_node=None, score=None)
        del row["node_id"]
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=page)))
    assert api.result_notices("demo", GEN) == {"status": 200, "body": page}


def test_older_page_may_end_before_total_without_automatic_paging():
    page = _page()
    page["total"] = 20
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=page)))
    assert api.result_notices("demo", GEN, cursor="opaque") == {"status": 200, "body": page}


def test_page_refuses_more_items_than_requested_limit():
    page = _page()
    page.update(total=2, items=page["items"] + [{**page["items"][0], "id": "node:1:0", "node_id": 1}])
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=page)))
    result = api.result_notices("demo", GEN, limit=1)
    assert result["outcome"] == "unavailable" and "body" not in result


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf")])
def test_page_refuses_nonfinite_json_number_without_an_exception(score):
    page = _page()
    page["items"][0]["score"] = score
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=json.dumps(page).encode())))
    result = api.result_notices("demo", GEN)
    assert result["outcome"] == "unavailable" and "body" not in result
