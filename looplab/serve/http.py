"""One JSON-body parser for the control plane (doc 25 SR-05).

The same eight lines — parse, 400 on a decode failure, 400 on a non-object — were written out four
times as module-level `_json_object` copies and re-inlined about ten more times across the routers.
Three of the copies carried a docstring saying which OTHER router they mirrored, which is the
duplication documenting itself instead of being removed.

Two properties differ per route and are therefore arguments, not variants:

* the **subject noun** in the 400 message (``"control body"``, ``"settings payload"``, …). These
  strings are part of the HTTP contract — the suite matches on them — so the helper renders them
  rather than flattening every route to one wording;
* whether an **absent body** means ``{}`` or a 400. A route whose every field has a default may
  legitimately be POSTed with nothing at all; one that requires a fence field may not.

Everything else is one implementation. In particular the catch is deliberately broad: a body the
client sent is a CLIENT error, and no shape of it may surface as a 500 with a traceback. That was
already the rule two routes had written down locally (`routers/misc.py`'s settings/secret writers);
making it the parser's rule is what stops the next copy from being narrower by accident.
``CancelledError`` derives from ``BaseException`` and so is still propagated, as it must be.

Routes whose 400 body is a STRUCTURED dict rather than a string (`/api/start` and its preflight
answer ``{"code": "invalid_launch_request", "field_errors": {}}``) keep their own parse: that is a
different response contract, not another copy of this one.
"""
from __future__ import annotations

import json


def _bad_request(message: str):
    """A 400, with ``HTTPException`` imported LAZILY on the failure path only.

    `serve/server.py` re-exports from routers at import time and must stay importable WITHOUT the
    [ui] extra, so that `make_app` can answer with "pip install looplab[ui]" instead of an ImportError
    traceback. A module-level `from fastapi import ...` here would be pulled in through those
    re-exports and break that (`tests/test_event_types.py` catches it). The same reason
    `serve/engine_proc.py` imports fastapi inside its functions.
    """
    from fastapi import HTTPException

    return HTTPException(400, message)


def json_object_bytes(raw: bytes, subject: str = "request body", *,
                      absent_is_empty: bool = False) -> dict:
    """Decode already-read bytes as a JSON object, or raise the shared 400.

    Separate from `json_object` for the routes that must read the body themselves — a byte-capped
    streaming read enforces a route-specific 413 BEFORE anything is decoded, which is policy the
    parser has no business owning.
    """
    if absent_is_empty and not raw:
        return {}
    try:
        body = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - see module docstring: never a 500 for client bytes
        raise _bad_request(f"{subject} must be valid JSON") from exc
    if not isinstance(body, dict):
        raise _bad_request(f"{subject} must be a JSON object")
    return body


async def json_object(request, subject: str = "request body", *,
                      absent_is_empty: bool = False) -> dict:
    """Parse a request body as a JSON object, or fail with the shared 400.

    A non-JSON or non-object body (a bare ``[]``, say) yields a clean 400 here instead of a 500
    from a later ``body.get(...)`` — the reason every one of these copies existed.
    """
    try:
        raw = await request.body()
    except Exception as exc:  # noqa: BLE001 - an unreadable body is still the client's
        raise _bad_request(f"{subject} must be valid JSON") from exc
    return json_object_bytes(raw, subject, absent_is_empty=absent_is_empty)


def comment_filter_invalid() -> "HTTPException":  # noqa: F821 - see `_bad_request`
    """`node_id` and `node_generation` name ONE experiment lifecycle and are meaningless apart.

    Accepting one without the other would silently widen the filter to "every attempt of that node",
    which is the opposite of what a caller pinning a lifecycle asked for. Shared because the owner
    and reviewer comment feeds must refuse identically — a reviewer who can filter more loosely than
    the owner sees comments the owner's own view would have excluded.
    """
    from fastapi import HTTPException

    return HTTPException(400, {
        "code": "comment_filter_invalid",
        "message": "node_id and node_generation must be supplied together",
        "remediation": "select an exact experiment lifecycle or remove both filters",
    })


def comment_cursor_error(exc) -> "HTTPException":  # noqa: F821 - see `_bad_request`
    """A cursor that is malformed (400) or belongs to another generation/scope (409).

    The split matters to a client: 400 says the cursor was never valid, 409 says it was valid for a
    run state that has since moved — only the second is worth re-fetching page one for. Both comment
    surfaces must answer the same way, which is why this stopped being a private helper of one of
    them (doc 25 SR-09).
    """
    from fastapi import HTTPException

    return HTTPException(409 if exc.stale else 400, {
        "code": "comment_cursor_stale" if exc.stale else "invalid_comment_cursor",
        "message": str(exc),
        "remediation": "refresh comments from the first page",
    })


def request_body_contract(*models) -> dict:
    """Publish a raw-Request body's schema WITHOUT letting FastAPI parse it.

    Both settings-shaped routes — `PUT /api/settings` and `PUT /api/runs/{id}/config` — accept a
    strict envelope and a legacy bare-mapping body, and both must keep parsing the body themselves:
    declaring the union as a Pydantic body parameter would turn their established malformed-JSON
    400 into FastAPI's 422, silently changing a contract clients already handle. So the models
    document the wire while the handler keeps the compatibility parse.

    That reasoning was written out twice, once per route, along with the anyOf assembly. One model
    publishes its schema directly; several publish an `anyOf`, which is what makes the legacy body
    discoverable rather than merely tolerated (doc 25 SR-14).
    """
    variants = [model.model_json_schema() for model in models]
    schema = variants[0] if len(variants) == 1 else {"anyOf": variants}
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }
