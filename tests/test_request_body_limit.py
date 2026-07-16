"""Uniform API request-body envelope, including chunked transfers."""
from __future__ import annotations

import anyio
from fastapi.testclient import TestClient

from looplab.serve.server import (
    _API_REQUEST_BODY_MAX,
    _APIRequestBodyLimitMiddleware,
    make_app,
)


def test_declared_oversize_api_body_is_rejected_before_router(tmp_path):
    client = TestClient(make_app(tmp_path))
    response = client.put(
        "/api/settings",
        content=b"x" * (_API_REQUEST_BODY_MAX + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "API request body is too large"}


def test_chunked_body_without_content_length_is_counted():
    called = False
    sent = []
    chunks = iter([
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ])

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/settings", "headers": []}
    anyio.run(_APIRequestBodyLimitMiddleware(downstream, max_bytes=5), scope, receive, send)
    assert called is False
    assert sent[0]["status"] == 413
    assert b"too large" in sent[1]["body"]


def test_single_oversize_asgi_chunk_is_rejected_before_downstream():
    called = False
    sent = []

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return {"type": "http.request", "body": b"x" * 4096, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/settings", "headers": []}
    anyio.run(_APIRequestBodyLimitMiddleware(downstream, max_bytes=16), scope, receive, send)
    assert called is False
    assert sent[0]["status"] == 413


def test_non_stripping_root_path_api_body_is_still_counted():
    called = False
    sent = []

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return {"type": "http.request", "body": b"abcdef", "more_body": False}

    async def send(message):
        sent.append(message)

    prefix = "/user/alice/proxy/8000"
    scope = {
        "type": "http",
        "method": "POST",
        "root_path": prefix,
        "path": f"{prefix}/api/settings",
        "headers": [],
    }
    anyio.run(_APIRequestBodyLimitMiddleware(downstream, max_bytes=5), scope, receive, send)
    assert called is False
    assert sent[0]["status"] == 413
    assert b"too large" in sent[1]["body"]


def test_non_stripping_root_path_non_api_body_is_not_buffered():
    received = []
    sent = []

    async def downstream(scope, receive, send):
        received.append(await receive())

    async def receive():
        return {"type": "http.request", "body": b"abcdef", "more_body": False}

    async def send(message):
        sent.append(message)

    prefix = "/user/alice/proxy/8000"
    scope = {
        "type": "http",
        "method": "POST",
        "root_path": prefix,
        "path": f"{prefix}/assets/app.js",
        "headers": [],
    }
    anyio.run(_APIRequestBodyLimitMiddleware(downstream, max_bytes=5), scope, receive, send)
    assert received == [{"type": "http.request", "body": b"abcdef", "more_body": False}]
    assert sent == []
