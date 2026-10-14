"""Artifact previews (doc 77 §3): `/artifact/raw` serves raster images only, `/artifact/meta`
describes any file without interpreting it, and both keep every `/artifact` fence."""
from __future__ import annotations

import io
import json
import os
import struct
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve import artifacts as art  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _run(tmp_path: Path) -> tuple[Path, Path]:
    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    repo = tmp_path / "repo"
    repo.mkdir()
    (rd / "task.snapshot.json").write_text(
        json.dumps({"kind": "repo", "editable_path": str(repo)}), encoding="utf-8")
    return rd, repo


def _gen(client) -> str:
    return client.get("/api/runs/demo/state").json()["generation"]


def _get(client, route, path, root="editable:."):
    return client.get(f"/api/runs/demo/artifact/{route}",
                      params={"root": root, "path": path, "expected_generation": _gen(client)})


def _safetensors(header: dict, payload: bytes = b"") -> bytes:
    body = json.dumps(header).encode()
    return struct.pack("<Q", len(body)) + body + payload


def test_raw_serves_matching_raster_images_with_sandbox_headers(tmp_path):
    _rd, repo = _run(tmp_path)
    (repo / "plot.png").write_bytes(PNG)
    client = TestClient(make_app(tmp_path))
    r = _get(client, "raw", "plot.png")
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]
    assert "default-src 'none'" in r.headers["content-security-policy"]


@pytest.mark.parametrize("name,data", [
    ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
    ("page.html", b"<html><script>alert(1)</script></html>"),
    ("fake.png", b"<html><script>alert(1)</script></html>"),     # suffix lies about the content
    ("real.jpg", PNG),                                              # magic and suffix disagree
    ("doc.pdf", b"%PDF-1.4\n"),
])
def test_raw_refuses_everything_but_a_matching_raster(tmp_path, name, data):
    _rd, repo = _run(tmp_path)
    (repo / name).write_bytes(data)
    client = TestClient(make_app(tmp_path))
    r = _get(client, "raw", name)
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "preview_unsupported"
    assert b"<script>" not in r.content


def test_raw_refuses_an_oversize_image(tmp_path, monkeypatch):
    _rd, repo = _run(tmp_path)
    (repo / "big.png").write_bytes(PNG + b"\x00" * 100)
    monkeypatch.setattr(art, "ART_RAW_MAX_BYTES", 16)
    from looplab.serve.routers import runs as runs_router
    monkeypatch.setattr(runs_router, "ART_RAW_MAX_BYTES", 16)
    client = TestClient(make_app(tmp_path))
    r = _get(client, "raw", "big.png")
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "preview_too_large"


@pytest.mark.parametrize("route", ["raw", "meta"])
def test_previews_keep_the_traversal_symlink_and_secret_fences(tmp_path, route):
    _rd, repo = _run(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    os.symlink(outside, repo / "link.png")
    (repo / ".env").write_bytes(b"HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz0123\n")
    client = TestClient(make_app(tmp_path))
    assert _get(client, route, "../outside.png").status_code == 404
    assert _get(client, route, "link.png").status_code == 404        # resolves outside the root
    assert _get(client, route, ".env").status_code == 404            # secret-named file
    assert _get(client, route, "x.png", root="nope").status_code == 404
    r = client.get(f"/api/runs/demo/artifact/{route}",
                   params={"root": "editable:.", "path": "x.png"})
    assert r.status_code == 422                                      # the generation is required


def test_meta_hashes_and_reads_a_safetensors_header_without_loading_tensors(tmp_path):
    _rd, repo = _run(tmp_path)
    header = {"__metadata__": {"format": "pt"},
              "w": {"dtype": "F32", "shape": [3, 4], "data_offsets": [0, 48]},
              "b": {"dtype": "BF16", "shape": [4], "data_offsets": [48, 56]}}
    blob = _safetensors(header, b"\x00" * 56)
    (repo / "model.safetensors").write_bytes(blob)
    client = TestClient(make_app(tmp_path))
    meta = _get(client, "meta", "model.safetensors").json()
    import hashlib
    assert meta["size"] == len(blob)
    assert meta["sha256"] == hashlib.sha256(blob).hexdigest()
    assert meta["kind"] == "safetensors"
    st = meta["safetensors"]
    assert st["tensor_count"] == 2 and st["parameter_count"] == 16
    assert st["dtypes"] == {"F32": 1, "BF16": 1}
    assert st["metadata"] == {"format": "pt"}
    assert meta["inline_image"] is False


def test_meta_lists_a_torch_zip_without_unpickling(tmp_path):
    _rd, repo = _run(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # A pickle that would run code if anything loaded it: the summary must only NAME it.
        zf.writestr("archive/data.pkl", b"cos\nsystem\n(S'touch /tmp/pwned'\ntR.")
        zf.writestr("archive/data/0", b"\x00" * 16)
    (repo / "ckpt.pt").write_bytes(buf.getvalue())
    client = TestClient(make_app(tmp_path))
    meta = _get(client, "meta", "ckpt.pt").json()
    assert meta["kind"] == "zip"
    assert meta["zip"]["members"] == 2 and meta["zip"]["pickle_inside"] is True
    assert {e["name"] for e in meta["zip"]["entries"]} == {"archive/data.pkl", "archive/data/0"}


def test_meta_reports_images_as_inline_previewable(tmp_path):
    _rd, repo = _run(tmp_path)
    (repo / "plot.png").write_bytes(PNG)
    client = TestClient(make_app(tmp_path))
    meta = _get(client, "meta", "plot.png").json()
    assert meta["kind"] == "png" and meta["inline_image"] is True and meta["mime"] == "image/png"


def test_pure_readers_are_total_over_malformed_input():
    assert art.safetensors_summary(io.BytesIO(b"\xff" * 8), 8) is None
    assert art.safetensors_summary(io.BytesIO(struct.pack("<Q", 10) + b"not json!!"), 18) is None
    bad_shape = _safetensors({"w": {"dtype": "F32", "shape": ["x"], "data_offsets": [0, 0]}})
    assert art.safetensors_summary(io.BytesIO(bad_shape), len(bad_shape)) is None
    assert art.zip_summary(io.BytesIO(b"PK\x03\x04garbage")) is None
    npy = b"\x93NUMPY\x01\x00" + struct.pack("<H", 70) + (
        b"{'descr': '<f4', 'fortran_order': False, 'shape': (2, 3), }").ljust(70)
    assert art.npy_summary(io.BytesIO(npy)) == {"dtype": "<f4", "shape": [2, 3],
                                                 "fortran_order": False}
    evil = b"\x93NUMPY\x01\x00" + struct.pack("<H", 40) + b"__import__('os').system('x')".ljust(40)
    assert art.npy_summary(io.BytesIO(evil)) is None
    assert art.sniff_kind(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert art.raster_mime("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert art.raster_mime("a.svg", b"<svg") is None
