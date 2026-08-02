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
