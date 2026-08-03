"""One failure epilogue for the request-path Boss LLM endpoints (doc 25 SR-15).

`chat_compact`, `chat` and `suggest` each closed with the same ten lines. Both halves of that block
are load-bearing and neither is visible from the call site, which is why one copy drifting would be
hard to notice: a run-generation conflict must REACH the client as a conflict, and everything else
must soft-fail as a 200 with the provider's raw text stripped out.

`command` deliberately does not share it — it computes on a background job thread and returns a plain
dict, so it cannot raise into a client at all.
"""
from __future__ import annotations

import inspect

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from looplab.serve.routers import boss  # noqa: E402

_LEAK = "HTTP 401 at https://user:secret@provider.example/v1/chat?token=hidden"


def test_a_generation_conflict_is_re_raised_not_swallowed():
    """The run moved under the request. Answering 200 `ok:false` would tell the UI the turn merely
    failed and invite a retry against the same stale generation."""
    conflict = HTTPException(409, {"code": "run_generation_changed", "detail": _LEAK})
    with pytest.raises(HTTPException) as info:
        boss._boss_failure_response(conflict)
    assert info.value.status_code == 409
    assert info.value.detail["code"] == "run_generation_changed"
    assert _LEAK not in str(info.value.detail), "the sanitized copy replaces the raw detail"
    assert "Reload the current run generation" in info.value.detail["remediation"]


def test_the_other_allow_listed_conflict_is_re_raised_too():
    with pytest.raises(HTTPException):
        boss._boss_failure_response(
            HTTPException(409, {"code": "run_generation_unavailable"}))


def test_an_unlisted_http_failure_soft_fails_rather_than_leaking_its_status():
    """Only the allow-listed lifecycle codes are the client's to act on. Anything else is treated
    like an outage, because reflecting an arbitrary upstream status here would turn a provider's
    401/500 into the Boss route's own."""
    response = boss._boss_failure_response(HTTPException(500, {"code": "whatever"}))
    assert isinstance(response, JSONResponse) and response.status_code == 200


def test_an_ordinary_outage_soft_fails_with_the_provider_text_stripped():
    """"No model configured" and "endpoint unreachable" are the normal offline case for these
    routes, not server errors — but the body must not carry the URL or credentials."""
    response = boss._boss_failure_response(RuntimeError(_LEAK))
    assert isinstance(response, JSONResponse) and response.status_code == 200
    body = response.body.decode()
    assert '"ok":false' in body.replace(" ", "")
    for secret in ("secret", "provider.example", "token=hidden"):
        assert secret not in body, f"{secret!r} reached the response body"


def test_the_accounting_failure_keeps_its_own_kind():
    """A pending durable cost write is the server's own state, not a provider outage — the UI shows
    a different remediation for it."""
    from looplab.serve.paid_work import RunCostAccountingPending

    response = boss._boss_failure_response(RunCostAccountingPending("x"))
    assert b"accounting_pending" in response.body


@pytest.mark.parametrize("endpoint", ["chat_compact", "chat", "suggest"])
def test_each_request_path_endpoint_uses_the_shared_epilogue(endpoint):
    source = inspect.getsource(boss)
    body = source[source.index(f"async def {endpoint}("):]
    body = body[:body.index("\n    @")] if "\n    @" in body else body
    assert "_boss_failure_response(" in body, f"{endpoint} does not use the shared epilogue"
    assert "_sanitized_domain_http_exception" not in body, f"{endpoint} re-derives the sanitization"


def test_the_epilogue_exists_in_exactly_one_place():
    source = inspect.getsource(boss)
    # `_sanitized_domain_http_exception` is CALLED twice: once by the request-path epilogue and once
    # by its background-thread counterpart. A third call means an endpoint grew its own again.
    assert source.count("_sanitized_domain_http_exception(exc)") == 2
    assert source.count('JSONResponse({"ok": False, **_safe_boss_failure(') == 1


def test_the_background_counterpart_stays_separate_and_never_raises():
    """It runs on a job thread with no client to raise into, so the same conflict has to come back as
    a DATA row the UI can render. Folding the two together would either lose the conflict there or
    make the request path swallow it."""
    row = boss._background_http_failure(
        HTTPException(409, {"code": "run_generation_changed", "detail": _LEAK}))
    assert isinstance(row, dict) and row["ok"] is False
    assert row["code"] == "run_generation_changed" and row["error_kind"] == "run_state_conflict"
    assert _LEAK not in str(row)
    unlisted = boss._background_http_failure(HTTPException(500, {"code": "whatever"}))
    assert unlisted["code"] == "background_request_rejected"


def test_the_reason_they_are_separate_is_written_down():
    doc = boss._boss_failure_response.__doc__ or ""
    assert "background job thread" in doc and "_background_http_failure" in doc
