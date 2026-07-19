"""Production UI static-delivery contracts: compression, caching, and HTML freshness."""
from __future__ import annotations

import json
from urllib.parse import urljoin

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.server import (  # noqa: E402
    _IMMUTABLE_ASSET_CACHE,
    _is_live_sse_path,
    make_app,
)


def _dist(tmp_path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head></head><body>LoopLab</body></html>", encoding="utf-8")
    payload = ("window.looplabBundle = 'compressible-production-asset';\n" * 400).encode("utf-8")
    (assets / "app-a1b2c3d4.js").write_bytes(payload)
    (assets / "runtime.js").write_text("window.runtime = true;\n", encoding="utf-8")
    manifest_dir = dist / ".vite"
    manifest_dir.mkdir()
    (manifest_dir / "manifest.json").write_text(json.dumps({
        "src/main.jsx": {"file": "assets/app-a1b2c3d4.js", "isEntry": True},
        "src/runtime.js": {"file": "assets/runtime.js"},
    }), encoding="utf-8")
    return dist, payload


def test_assets_are_gzipped_and_immutable_while_open_index_revalidates(tmp_path, monkeypatch):
    dist, payload = _dist(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_DIST", str(dist))
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path / "runs"))

    response = client.get(
        "/assets/app-a1b2c3d4.js", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.content == payload  # TestClient transparently decompresses the body.
    assert response.headers["Content-Encoding"] == "gzip"
    assert "accept-encoding" in {
        item.strip().lower() for item in response.headers["Vary"].split(",")}
    assert response.headers["Cache-Control"] == _IMMUTABLE_ASSET_CACHE
    assert int(response.headers["Content-Length"]) < len(payload)

    conditional = client.get(
        "/assets/app-a1b2c3d4.js",
        headers={"Accept-Encoding": "gzip", "If-None-Match": response.headers["ETag"]},
    )
    assert conditional.status_code == 304
    assert conditional.headers["Cache-Control"] == _IMMUTABLE_ASSET_CACHE
    assert client.get("/assets/runtime.js").headers["Cache-Control"] == "no-cache", (
        "manifest membership alone must not make a stable, non-content-addressed URL immutable")
    assert client.get("/").headers["Cache-Control"] == "no-cache"


def test_live_sse_route_guard_supports_proxy_prefixes_without_matching_lookalikes():
    assert _is_live_sse_path("/prefix/api/runs/demo/events")
    assert _is_live_sse_path("/prefix/api/assistant/sessions/demo/message_stream")
    assert not _is_live_sse_path("/prefix/api/runs/demo/events/export")
    assert not _is_live_sse_path("/prefix/api/assistant/sessions/demo/message_stream/export")


def test_token_protected_index_remains_no_store(tmp_path, monkeypatch):
    dist, _ = _dist(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_DIST", str(dist))
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")

    response = TestClient(make_app(tmp_path / "runs")).get(
        "/", headers={"Sec-Fetch-Dest": "document"})

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_review_shells_keep_relative_assets_inside_a_proxy_mount(tmp_path, monkeypatch):
    dist, _ = _dist(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_DIST", str(dist))
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path / "runs"))

    canonical = client.get("/review")
    assert canonical.status_code == 200
    assert '<base href="../">' not in canonical.text
    assert canonical.headers["cache-control"] == "no-store"
    assert canonical.headers["referrer-policy"] == "no-referrer"

    trailing = client.get("/review/")
    assert trailing.status_code == 200
    assert '<base href="../review">' in trailing.text
    assert trailing.headers["cache-control"] == "no-store"
    assert trailing.headers["referrer-policy"] == "no-referrer"

    mounted_review = "https://lab.example/user/alice/proxy/8765/review"
    mounted_root = "https://lab.example/user/alice/proxy/8765/"
    for relative in ("./assets/app-a1b2c3d4.js", "./index.html"):
        assert urljoin(mounted_review, relative).startswith(mounted_root)
        trailing_base = urljoin(f"{mounted_review}/", "../review")
        assert urljoin(trailing_base, relative).startswith(mounted_root)
    assert urljoin(urljoin(f"{mounted_review}/", "../review"), "#/run/demo") == (
        f"{mounted_review}#/run/demo")


