"""Security middleware must classify the same root_path-relative route as Starlette."""
from __future__ import annotations

import anyio

from looplab.serve.server import make_app


async def _request(app, method="GET", headers=()):
    prefix = "/user/alice/proxy/8000"
    sent = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "root_path": prefix,
        "path": f"{prefix}/api/runs", "raw_path": f"{prefix}/api/runs".encode(),
        "query_string": b"", "headers": list(headers),
        "client": ("testclient", 50000), "server": ("localhost", 80),
    }
    await app(scope, receive, send)
    return next(message["status"] for message in sent
                if message["type"] == "http.response.start")


def test_prefixed_owner_api_is_not_allowed_to_bypass_token(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    app = make_app(tmp_path)
    status = anyio.run(_request, app, "GET", ((b"host", b"localhost"),))
    assert status == 401


def test_prefixed_owner_mutation_is_not_allowed_to_bypass_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    app = make_app(tmp_path)
    status = anyio.run(
        _request, app, "POST",
        (
            (b"host", b"localhost"),
            (b"origin", b"https://attacker.example"),
            (b"x-looplab-token", b"owner-secret"),
            (b"content-length", b"0"),
        ),
    )
    assert status == 403
