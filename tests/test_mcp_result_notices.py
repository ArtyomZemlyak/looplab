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
                   "trust_flagged": False, "trust_advisory": False, "parent_trust_advisory": False,
                   "parents": [], "score_comparison": {"version": 1, "parent_count": 0, "status": "no_parent"},
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


@pytest.mark.parametrize("comparison,parents", [
    (None, []), ({}, []), ({"version": True, "parent_count": 0, "status": "no_parent"}, []),
    ({"version": 1, "parent_count": -1, "status": "no_parent"}, []),
    ({"version": 1, "parent_count": True, "status": "no_parent"}, []),
    ({"version": 1, "parent_count": 0, "status": "future"}, []),
    ({"version": 1, "parent_count": 1, "status": "same"}, []),
    ({"version": 1, "parent_count": 2, "status": "same"}, []),
    ({"version": 1, "parent_count": 0, "status": "no_parent"}, [None]),
    ({"version": 1, "parent_count": 1, "status": "same"},
     [{"node_id": 1, "attempt": 0, "score": .5, "comparability": "same"}]),
])
def test_incomplete_or_inconsistent_comparison_is_unavailable(comparison, parents):
    page = _page()
    page["items"][0].update(score_comparison=comparison, parents=parents)
    _refused_page(page)


def test_missing_comparison_metadata_is_not_legacy_permission_to_compare():
    page = _page()
    del page["items"][0]["score_comparison"]
    _refused_page(page)


@pytest.mark.parametrize("field", ["trust_flagged", "trust_advisory", "parent_trust_advisory"])
@pytest.mark.parametrize("value", [None, 0, "false", "missing"])
def test_missing_or_malformed_trust_evidence_is_unavailable(field, value):
    page = _page()
    if value == "missing":
        del page["items"][0][field]
    else:
        page["items"][0][field] = value
    _refused_page(page)


@pytest.mark.parametrize("changes", [
    {"trust_flagged": True, "trust_advisory": True}, {"parent_trust_advisory": True},
])
def test_contradictory_trust_evidence_is_unavailable(changes):
    page = _page()
    page["items"][0].update(changes)
    _refused_page(page)


@pytest.mark.parametrize("change", [
    {"feasible": False}, {"feasible": 1}, {"trust_flagged": True}, {"salvaged": True},
    {"violations": True}, {"violations": 1}, {"direction": "unknown"},
    {"score": None}, {"score": 1e308, "parents": [{"node_id": 1, "attempt": 0, "score": -1e308, "comparability": "same"}]},
    {"parents": [{"node_id": 1, "attempt": 0, "score": .5, "comparability": "unknown"}]},
])
def test_positive_comparison_requires_complete_consistent_evidence(change):
    page = _page()
    row = page["items"][0]
    row.update(feasible=True, trust_flagged=False, salvaged=False, violations=0, direction="min",
               parents=[{"node_id": 1, "attempt": 0, "score": .5, "comparability": "same"}],
               score_comparison={"version": 1, "parent_count": 1, "status": "same"})
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda req: httpx.Response(200, json=page)))
    assert api.result_notices("demo", GEN) == {"status": 200, "body": page}
    row.update(change)
    _refused_page(page)


@pytest.mark.parametrize("status", ["evaluated", "failed", "aborted", "finished"])
def test_valid_terminal_pages_preserve_evidence_and_extra_fields(status):
    page = _page()
    row = page["items"][0]
    row.update(status=status, future_metadata={"retained": True})
    if status in {"failed", "aborted"}:
        row["score"] = None
    if status == "finished":
        row.update(id="run", kind="run", attempt=None, selected_node=None, score=None,
                   evaluated=0, failed=0, direction="min", objective="loss", caveats=[])
        del row["node_id"]
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=page)))
    assert api.result_notices("demo", GEN) == {"status": 200, "body": page}


@pytest.mark.parametrize("change", [
    {"trust_advisory": None}, {"trust_advisory": 0}, {"trust_advisory": "false"},
    {"selected_node": None, "attempt": None}, {"selected_node": None, "attempt": 0, "score": None},
    {"selected_node": True}, {"attempt": True}, {"score": None},
    {"evaluated": True}, {"failed": -1}, {"direction": "unknown"},
    {"objective": None}, {"caveats": None}, {"caveats": [None]},
    {"caveats": ["trust_flagged"], "trust_advisory": False},
])
def test_run_receipt_refuses_incomplete_or_inconsistent_selected_evidence(change):
    page = _page()
    page["items"][0].update(id="run", kind="run", status="finished", selected_node=0, attempt=0,
                            evaluated=1, failed=0, direction="min", objective="loss", caveats=[])
    page["items"][0].update(change)
    _refused_page(page)


@pytest.mark.parametrize("field", ["trust_advisory", "selected_node", "attempt", "evaluated", "failed", "caveats"])
def test_run_receipt_does_not_default_missing_evidence_to_clean(field):
    page = _page()
    page["items"][0].update(id="run", kind="run", status="finished", selected_node=0, attempt=0,
                            evaluated=1, failed=0, direction="min", objective="loss", caveats=[])
    del page["items"][0][field]
    _refused_page(page)


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
