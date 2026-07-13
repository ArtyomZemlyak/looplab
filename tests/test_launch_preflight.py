"""Fail-closed new-run preflight and frozen launch-input contract."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.server import make_app  # noqa: E402


def _toy() -> dict:
    return {"benchmark": "quadratic", "goal": "minimize the objective", "direction": "min"}


def _repo(repo: Path, **extra) -> dict:
    return {
        "goal": "maximize the score",
        "direction": "max",
        "repo": str(repo),
        "cmd": {
            "command": ["python", "score.py"],
            "metric": {"reader": "stdout_json", "key": "score"},
        },
        **extra,
    }


def test_preflight_is_read_only_and_returns_effective_preview(tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    spawned = []
    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine",
                        lambda *args, **kwargs: spawned.append((args, kwargs)))
    before = sorted(path.name for path in tmp_path.iterdir())

    response = client.post("/api/start/preflight", json={
        "run_id": "preview-only",
        "task": _toy(),
        "settings": {"max_nodes": 4},
    })

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and len(body["validation_token"]) == 64
    assert body["preview"]["task"]["kind"] == "quadratic"
    assert body["preview"]["settings"]["max_nodes"] == 4
    assert body["preview"]["source"] == "inline"
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert not (tmp_path / "preview-only").exists()
    assert not spawned


def test_validation_token_binds_clean_chat_deterministically(tmp_path):
    client = TestClient(make_app(tmp_path))
    base = {
        "run_id": "chat-bound",
        "task": _toy(),
        "chat": [{"role": "user", "content": "create exactly this run"}],
    }
    first = client.post("/api/start/preflight", json=base).json()["validation_token"]
    second = client.post("/api/start/preflight", json=base).json()["validation_token"]
    changed = client.post("/api/start/preflight", json={
        **base,
        "chat": [{"role": "user", "content": "edited creation context"}],
    }).json()["validation_token"]

    assert first == second
    assert changed != first


@pytest.mark.parametrize("payload", [
    {"run_id": "none"},
    {"run_id": "both", "task": {"kind": "quadratic"}, "task_file": "task.json"},
])
def test_preflight_requires_exactly_one_task_source(tmp_path, payload):
    response = TestClient(make_app(tmp_path)).post("/api/start/preflight", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_task_source"
    assert not (tmp_path / payload["run_id"]).exists()


def test_preflight_loads_and_validates_task_file_without_side_effects(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"kind":"not-real"}', encoding="utf-8")
    client = TestClient(make_app(tmp_path))

    response = client.post("/api/start/preflight", json={
        "run_id": "invalid-file",
        "task_file": str(invalid),
    })

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_task"
    assert not (tmp_path / "invalid-file").exists()


@pytest.mark.parametrize(("settings", "field"), [
    ({"max_nodez": 4}, "settings.max_nodez"),
    ({"llm_api_key": "must-not-transit-here"}, "settings.llm_api_key"),
    ({"max_nodes": 0}, "settings.max_nodes"),
])
def test_preflight_rejects_unknown_secret_and_invalid_settings(tmp_path, settings, field):
    response = TestClient(make_app(tmp_path)).post("/api/start/preflight", json={
        "run_id": "bad-settings",
        "task": _toy(),
        "settings": settings,
    })

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_launch_settings"
    assert field in detail["field_errors"]
    assert not (tmp_path / "bad-settings").exists()


def test_preflight_checks_every_repo_path_scope(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    missing_ref = tmp_path / "missing-ref"
    missing_data = tmp_path / "missing-data"
    task = _repo(repo, editables=[{"name": "other", "path": str(other)}],
                 references=[{"name": "ref", "path": str(missing_ref)}],
                 data={"dataset": {"path": str(missing_data)}})

    response = TestClient(make_app(tmp_path)).post("/api/start/preflight", json={
        "run_id": "bad-paths",
        "task": task,
    })

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_task_paths"
    assert "task.references.0.path" in detail["field_errors"]
    assert "task.data.dataset.path" in detail["field_errors"]
    assert not (tmp_path / "bad-paths").exists()


def test_precedence_saved_then_file_then_explicit_and_file_backend_is_honest(tmp_path):
    (tmp_path / "ui_settings.json").write_text(json.dumps({
        "max_nodes": 2,
        "n_seeds": 2,
        "backend": "llm",
    }), encoding="utf-8")
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({
        "task": _toy(),
        "settings": {"max_nodes": 3, "n_seeds": 5, "backend": "toy"},
    }), encoding="utf-8")

    response = TestClient(make_app(tmp_path)).post("/api/start/preflight", json={
        "run_id": "precedence",
        "task_file": str(task_file),
        "settings": {"max_nodes": 7},
    })

    assert response.status_code == 200
    settings = response.json()["preview"]["settings"]
    assert settings["max_nodes"] == 7              # explicit launch edit wins
    assert settings["n_seeds"] == 5                # task-file setting wins saved UI
    assert settings["backend"] == "toy"            # task-file choice suppresses inference/UI default


def test_stale_validation_token_never_spawns_or_materializes(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(_toy()), encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    token = client.post("/api/start/preflight", json={
        "run_id": "stale",
        "task_file": str(source),
    }).json()["validation_token"]
    source.write_text(json.dumps({**_toy(), "goal": "changed after validation"}), encoding="utf-8")
    spawned = []
    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine",
                        lambda *args, **kwargs: spawned.append((args, kwargs)))

    response = client.post("/api/start", json={
        "run_id": "stale",
        "task_file": str(source),
        "validation_token": token,
    })

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "launch_validation_stale"
    assert not (tmp_path / "stale").exists()
    assert not spawned


def test_start_spawns_frozen_canonical_unified_copy_and_preserves_source(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text(json.dumps({
        "task": _toy(),
        "settings": {"max_nodes": 3, "n_seeds": 4},
    }), encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    captured = {}

    def fake_spawn(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env") or {}
        captured["run_dir"] = kwargs.get("run_dir")
        return None

    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine", fake_spawn)
    response = client.post("/api/start", json={
        "run_id": "frozen",
        "task_file": str(source),
        "settings": {"max_nodes": 9},
        "chat": [{"role": "user", "content": "create this run"}],
    })

    assert response.status_code == 200
    run_dir = tmp_path / "frozen"
    canonical_path = run_dir / "task.input.json"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    assert canonical["task"]["kind"] == "quadratic"
    assert canonical["settings"]["max_nodes"] == 9
    assert canonical["settings"]["n_seeds"] == 4
    assert "llm_api_key" not in canonical["settings"]
    assert Path(captured["args"][1]) == canonical_path
    assert str(source) not in captured["args"]
    assert captured["env"]["LOOPLAB_MAX_NODES"] == "9"
    meta = json.loads((run_dir / "ui_meta.json").read_text(encoding="utf-8"))
    assert meta == {"task_file": str(canonical_path), "source_task_file": str(source)}
    assert (run_dir / "chat.jsonl").exists()


def test_task_file_settings_reject_secret_and_unknown_fields(tmp_path):
    for index, settings in enumerate(({"llm_api_key": "secret"}, {"max_nodez": 4})):
        source = tmp_path / f"bad-settings-{index}.json"
        source.write_text(json.dumps({"task": _toy(), "settings": settings}), encoding="utf-8")
        response = TestClient(make_app(tmp_path)).post("/api/start/preflight", json={
            "run_id": f"bad-file-settings-{index}",
            "task_file": str(source),
        })
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_launch_settings"
