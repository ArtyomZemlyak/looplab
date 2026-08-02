"""The one JSON-body parser the control-plane routes share (doc 25 SR-05).

Four module-level copies plus ~10 inlined re-implementations of the same eight lines were replaced
by `serve/http.py`. The 400 message strings those copies produced are part of the HTTP contract and
NOTHING in the suite asserted them, so collapsing the copies was exactly the kind of change that can
silently reword a client-visible error. These tests pin the contract at both levels: the parser
itself, and the routes that now render their subject noun through it.
"""
from __future__ import annotations

import anyio
import pytest
from fastapi import HTTPException

from looplab.serve.http import json_object, json_object_bytes


def _detail(exc_info) -> str:
    return str(exc_info.value.detail)


class _Body:
    """The only part of a Request the parser touches."""

    def __init__(self, raw):
        self._raw = raw

    async def body(self) -> bytes:
        if isinstance(self._raw, BaseException):
            raise self._raw
        return self._raw


def _parse(raw, subject="request body", **kwargs) -> dict:
    """Drive the async parser from a sync test — the suite has no async-test plugin."""
    return anyio.run(lambda: json_object(_Body(raw), subject, **kwargs))


# ------------------------------------------------------------------ the object contract

def test_a_json_object_body_is_returned_unchanged():
    assert _parse(b'{"a": 1, "b": [2]}') == {"a": 1, "b": [2]}


@pytest.mark.parametrize("raw", [b"[]", b'"text"', b"7", b"null", b"true"])
def test_valid_json_that_is_not_an_object_is_a_400_not_a_later_attribute_error(raw):
    """The reason all four copies existed: a bare ``[]`` used to reach ``body.get(...)`` and surface
    as a 500."""
    with pytest.raises(HTTPException) as exc:
        _parse(raw)
    assert exc.value.status_code == 400
    assert _detail(exc) == "request body must be a JSON object"


@pytest.mark.parametrize("raw", [b"", b"{", b"not json", b"\xff\xfe\x00", b'{"a": nan'])
def test_undecodable_bytes_are_a_400(raw):
    with pytest.raises(HTTPException) as exc:
        _parse(raw)
    assert exc.value.status_code == 400
    assert _detail(exc) == "request body must be valid JSON"


def test_the_subject_noun_is_rendered_into_both_messages():
    """The per-route wording is contract, so the parser renders it rather than flattening it."""
    with pytest.raises(HTTPException) as decode:
        _parse(b"{", "settings payload")
    with pytest.raises(HTTPException) as shape:
        _parse(b"[]", "settings payload")
    assert _detail(decode) == "settings payload must be valid JSON"
    assert _detail(shape) == "settings payload must be a JSON object"


def test_an_absent_body_is_empty_options_only_where_the_route_says_so():
    """A route whose every field has a default may be POSTed with nothing; one carrying a fence
    field may not, and its 400 is what tells the caller to send one."""
    assert _parse(b"", absent_is_empty=True) == {}
    with pytest.raises(HTTPException):
        _parse(b"")
    # ...and `absent_is_empty` forgives ONLY emptiness, never malformed bytes.
    with pytest.raises(HTTPException) as exc:
        _parse(b"{", absent_is_empty=True)
    assert _detail(exc) == "request body must be valid JSON"


def test_an_unreadable_body_is_the_clients_error_not_a_server_traceback():
    """Two routes had already written this rule down locally for their own parse. Making it the
    parser's rule is what stops the next copy from being narrower by accident."""
    with pytest.raises(HTTPException) as exc:
        _parse(RuntimeError("stream broke"))
    assert exc.value.status_code == 400
    assert _detail(exc) == "request body must be valid JSON"


def test_cancellation_is_never_swallowed_into_a_400():
    """`CancelledError` is a BaseException; a shutdown must not be reported to the client as a
    malformed body."""
    with pytest.raises(BaseException) as exc:
        _parse(_Cancelled())
    assert isinstance(exc.value, BaseException) and not isinstance(exc.value, HTTPException)


class _Cancelled(BaseException):
    pass


def test_the_bytes_form_shares_every_rule_with_the_request_form():
    """Routes that must read the body themselves — a byte-capped streaming read enforcing a 413
    before anything is decoded — get the same decision table, not a fifth copy of it."""
    assert json_object_bytes(b'{"a": 1}') == {"a": 1}
    assert json_object_bytes(b"", absent_is_empty=True) == {}
    for raw, message in ((b"[]", "must be a JSON object"), (b"{", "must be valid JSON")):
        with pytest.raises(HTTPException) as exc:
            json_object_bytes(raw, "concept lens body")
        assert str(exc.value.detail) == f"concept lens body {message}"


def test_json_text_is_accepted_as_bytes_or_str():
    """`json.loads` takes both; a caller that decoded first must not get a different answer."""
    assert json_object_bytes('{"a": 1}') == json_object_bytes(b'{"a": 1}')


# ------------------------------------------------------------------ the routes' subject nouns

def test_no_router_reads_the_request_body_itself_to_re_derive_the_object_check():
    """A grep-level guard: the eight-line copy must not come back.

    `serve/http.py` owns reading and decoding a JSON request body, so a router that calls
    `request.json()`/`request.body()` itself is either re-implementing the parser or has a genuinely
    different contract — and the second kind is named here, with its reason, rather than left to
    look like the first.
    """
    import collections
    from pathlib import Path

    allowed = {
        # `/api/start` and `/api/start/preflight`: a STRUCTURED 400 body the launch client parses by
        # shape ({"code": "invalid_launch_request", "field_errors": {}}), not the string contract.
        "control.py": 2,
        # `PUT /api/authors/{name}`: a raw UTF-8 text body bounded by the author read cap. Never JSON.
        "misc.py": 1,
    }
    serve = Path(__file__).resolve().parents[1] / "looplab" / "serve"
    found = []
    for path in sorted(list((serve / "routers").glob("*.py")) + list(serve.glob("*.py"))):
        if path.name == "http.py":
            continue
        for index, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            if "await request.json()" in line or "await request.body()" in line:
                found.append((path.name, index))
    assert dict(collections.Counter(name for name, _ in found)) == allowed, (
        "body reads outside serve/http.py changed. Route it through `json_object` unless the route "
        f"has a genuinely different contract — and then name it above. Found: {found}")


# ------------------------------------------------------------------ the published body contract

def test_a_single_model_publishes_its_schema_directly():
    """`anyOf` with one branch would be technically correct and needlessly harder for a generator
    to read."""
    from looplab.serve.http import request_body_contract
    from pydantic import BaseModel

    class _One(BaseModel):
        value: str

    body = request_body_contract(_One)["requestBody"]
    assert body["required"] is True
    schema = body["content"]["application/json"]["schema"]
    assert "anyOf" not in schema and schema["properties"]["value"]["type"] == "string"


def test_several_models_publish_an_anyof_so_the_legacy_body_is_discoverable():
    """The legacy bare-mapping body is TOLERATED by the handler either way; publishing it is what
    makes it a documented contract instead of an accident a client discovers by trying."""
    from looplab.serve.http import request_body_contract
    from pydantic import BaseModel

    class _Envelope(BaseModel):
        settings: dict

    class _Legacy(BaseModel):
        model_config = {"extra": "allow"}

    schema = request_body_contract(
        _Envelope, _Legacy)["requestBody"]["content"]["application/json"]["schema"]
    assert [variant["title"] for variant in schema["anyOf"]] == ["_Envelope", "_Legacy"]


def test_the_settings_shaped_routes_keep_parsing_their_own_bodies():
    """The whole reason these schemas are `openapi_extra` and not body parameters: a Pydantic body
    would turn the established malformed-JSON 400 into FastAPI's 422. If a route ever gains a real
    body parameter, that contract changes silently — so assert the raw parse is still there."""
    from pathlib import Path

    serve = Path(__file__).resolve().parents[1] / "looplab" / "serve" / "routers"
    for name, route in (("misc.py", "put_settings"), ("runs.py", "put_run_config")):
        text = (serve / name).read_text(encoding="utf-8")
        assert "openapi_extra=request_body_contract(" in text, name
        assert f"def {route}(" in text, f"{name}: {route} was renamed; re-check its body contract"
