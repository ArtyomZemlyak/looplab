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
import re


_ETAG_TOKEN = re.compile(r'(?:W/)?"[!#-~\x80-\xff]*"')


def if_none_match(value: str | None, current: str) -> bool:
    """RFC 9110 weak comparison for one current validator against an ETag list.

    Conditional trace reads and the settings-schema endpoint share this grammar. Invalid list
    syntax is a cache miss, never permission to return a bodyless 304 for a validator we could not
    parse exactly.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip()
    if value == "*":
        return True
    target = current[2:] if current.startswith("W/") else current
    matched = False
    position = 0
    while position < len(value):
        while position < len(value) and value[position] in " \t,":
            position += 1
        if position == len(value):
            break
        match = _ETAG_TOKEN.match(value, position)
        if match is None:
            return False
        candidate = match.group(0)
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == target:
            matched = True
        position = match.end()
        while position < len(value) and value[position] in " \t":
            position += 1
        if position < len(value) and value[position] != ",":
            return False
    return matched


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


# THE REFUSAL VOCABULARY FOR INPUT THE SERVER COULD NOT READ (doc 50 SR-05; doc 52 §5.1 row 5).
#
# A route answers 503 WITH A `code` for a durable input it could not read — a snapshot that is
# unreadable, undecodable or not an object — never 500, and never the exception's own text. Six
# sites answered `HTTPException(500, f"… unreadable: {exc}")` where every sibling in
# `run_commands.py` answers 503 with a code, and two of them reflected the `OSError` text, host path
# included, to the browser. 500 is the FRAMEWORK's word for a fault in the server's own code (an
# uncaught exception): a client that receives it reports a crash and retries nothing, while a 503
# with a code says "the input is there and could not be read — look at it", which is a different
# remedy. The table is the one place the status, the sentence and the remedy live;
# `tests/test_refusal_vocabulary.py` scans `serve/` for a literal 500 and re-derives the used slugs
# against this table in both directions, so a seventh site cannot ship a status of its own.
REFUSALS: dict[str, tuple[int, str, str]] = {
    "config_snapshot_unreadable": (
        503, "the run configuration snapshot could not be read",
        "Inspect config.snapshot.json in the run directory: it is unreadable, or not valid UTF-8 "
        "JSON. Restore it from a backup or from another run of the same task."),
    "config_snapshot_not_object": (
        503, "the run configuration snapshot is not a JSON object",
        "Restore config.snapshot.json from a backup or from another run of the same task."),
    # THE COMMAND-LIFECYCLE SITES, added 2026-09-08. Eight of them raised 409/503 with an f-string
    # of the caught `OSError`, i.e. exactly what `refusal()`'s own docstring one screen down
    # forbids — driven: a stray regular file where the server wants its lock directory reflects
    # `[Errno 17] File exists: '/abs/host/path/.command-locks'` to the browser. They were invisible
    # to `test_no_route_answers_a_literal_500_for_input_it_could_not_read`, which walked only
    # `HTTPException(500, …)`, so the census that ended this class for the 500s never saw the
    # siblings raising other codes.
    "run_lock_path_unreadable": (
        409, "the run's command-lock path could not be validated",
        "The run directory's `.command-locks` entry is not a plain directory the server can "
        "resolve — most often a leftover file, a symlink, or a permission change. Remove or fix "
        "that entry inside the run directory and retry."),
    "run_path_unreadable": (
        409, "the run's command path could not be validated",
        "The run directory's `events.jsonl` or `.commands` entry is not a plain file/directory the "
        "server can resolve — most often a symlink or a permission change. Fix that entry inside "
        "the run directory and retry."),
    "run_record_unquarantinable": (
        503, "an unreadable command record could not be quarantined",
        "A damaged file under the run's `.commands` directory could not be moved aside. Check the "
        "run directory's permissions and free space, then retry."),
    "run_command_locking_unsupported": (
        503, "the filesystem under the run directory does not support command locking",
        "The run directory is on a filesystem whose advisory locks the server cannot take (some "
        "network and FUSE mounts). Move the run directory to local storage, or run the server on "
        "the host that owns the mount."),
    "run_claim_unretirable": (
        503, "a run's start or spawn claim could not be retired",
        "The claim file under the run directory could not be removed. Check the run directory's "
        "permissions and free space, then retry; the claim is re-checked on every attempt."),
}


def refusal(slug: str) -> "HTTPException":  # noqa: F821 - see `_bad_request`
    """The one refusal for a `REFUSALS` slug: its status and a structured body carrying the slug.

    Never interpolate the exception: an `OSError`'s text carries the host path, and the body goes
    to the browser and into every export of it."""
    from fastapi import HTTPException

    status, message, remediation = REFUSALS[slug]
    return HTTPException(status, {"code": slug, "message": message, "remediation": remediation})


# THE GENERATION FENCE'S ONE REFUSAL (doc 25 SR-09).
#
# `{"code": "run_generation_changed", "expected_generation": …, "current_generation": …, "message":
# …, "remediation": …}` was hand-assembled at 26 sites across 10 serve files, and the copies had
# already drifted: some spelled `current_generation` with an `or None`, some without, some carried no
# remediation at all, one publishes the SAME fact under the key `actual_generation`. That envelope is
# a WIRE CONTRACT — twelve `ui/src` modules branch on the code, and `expected_generation`/
# `current_generation` are what a client CASes on next — so a copy that drops a field breaks the
# NEXT fenced write at a call site with no visible connection to the one that dropped it.
#
# What legitimately differs per site is the SENTENCE (which read or write the run outran) and the
# REMEDY (which view the operator reloads), so those are arguments; the code and the status are not.
# The two fence fields are OMITTED rather than null when a site genuinely has no generation to name
# (the comment feeds, whose 409 says only "the run moved while this was projected"), because a
# client cannot tell a null it must ignore from a null it should have received.
#
# THE LITERAL SURVIVES IN FOUR KINDS OF PLACE AND NONE IS A COPY OF THIS ENVELOPE, which is why the
# open-item marker was bound to this function's NAME rather than to the string:
#   * `run_commands.py::_generation_changed_error` builds a durable COMMAND RECORD's error object
#     (`_error(...)` with `retryable`), not an HTTP body — a record a client polls, not a refusal;
#   * `deletion_service.py` raises it through that module's OWN shared `_detail(...)` builder, one
#     code among ~20 in a deletion-receipt envelope that always carries `retryable` and
#     `operation_id`. It is already shared from one place; folding it into this one would change
#     that surface's wire shape, which is the opposite of what SR-09 asked for;
#   * `routers/boss.py` and `serve/assistant.py` READ the code off a caught exception to classify
#     it, and `trace_clear.py` writes `run_generation_changed_after_pending` as a RECEIPT reason.
RUN_GENERATION_CHANGED = "run_generation_changed"

#: Distinguishes "this site names no generation" from "this site names None" — see above.
_OMITTED = object()


def generation_conflict(message: str, *, expected=_OMITTED, current=_OMITTED,
                        remediation: str = "", **extra) -> "HTTPException":  # noqa: F821
    """The 409 every generation fence raises. `extra` carries a site's own additional identity.

    `expected`/`current` are the generation the caller named and the one the run actually has;
    passing neither omits both fields, which is what the two comment surfaces do. `remediation` is
    omitted when empty rather than sent as "", because the field is advice and an empty string reads
    as advice that was given and was blank.
    """
    from fastapi import HTTPException

    detail: dict = {"code": RUN_GENERATION_CHANGED}
    if expected is not _OMITTED:
        detail["expected_generation"] = expected
    if current is not _OMITTED:
        detail["current_generation"] = current
    detail["message"] = message
    if remediation:
        detail["remediation"] = remediation
    detail.update(extra)
    return HTTPException(409, detail)


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
