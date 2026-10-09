"""The UI's offline demo and `examples/demo.yaml` are one demo (doc 74 EB-24).

The first-run landing offers "Try the offline demo" as an ordinary launch card over a fixed spec,
`ui/src/offlineDemo.json`; the entry pages tell a terminal user to `looplab run examples/demo.yaml`.
A reader who tries both must get the same run, so the task and settings are held equal here, and the
spec is put through the server's own launch validation — a demo the server refuses would be a dead
button on the one screen every new user sees.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _spec():
    return json.loads((ROOT / "ui" / "src" / "offlineDemo.json").read_text(encoding="utf-8"))


def test_the_ui_demo_spec_is_the_demo_file():
    demo = yaml.safe_load((ROOT / "examples" / "demo.yaml").read_text(encoding="utf-8"))
    spec = _spec()
    assert spec["task"] == demo["task"]
    assert spec["settings"] == demo["settings"]
    assert spec["settings"]["backend"] == "toy", "the demo must never need a model"


def test_the_server_validates_the_ui_demo_spec(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app

    spec = _spec()
    body = {"run_id": spec["run_id"], "task": spec["task"], "settings": spec["settings"]}
    client = TestClient(make_app(tmp_path))
    verdict = client.post("/api/validate", json=body)
    assert verdict.status_code == 200, verdict.text
    assert verdict.json()["ok"] is True, verdict.json()
