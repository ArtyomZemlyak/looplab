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


def test_preflight_rejects_oversized_task_file(tmp_path, monkeypatch):
    """VAL-2: preflight slurps the task_file whole (load_document + fingerprint), so an unbounded file
    must be rejected before the read — otherwise a multi-GB path hangs the worker / exhausts memory."""
    import looplab.serve.launch as launch
    monkeypatch.setattr(launch, "_MAX_TASK_FILE_BYTES", 64)
    big = tmp_path / "big.json"
    big.write_text('{"kind":"quadratic"}' + " " * 200, encoding="utf-8")  # valid JSON, over the cap
    response = TestClient(make_app(tmp_path)).post("/api/start/preflight", json={
        "run_id": "big-file", "task_file": str(big),
    })
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "task_file_too_large"
    assert not (tmp_path / "big-file").exists()


@pytest.mark.parametrize(("settings", "field"), [
    ({"max_nodez": 4}, "settings.max_nodez"),
    ({"llm_api_key": "must-not-transit-here"}, "settings.llm_api_key"),
    ({"max_nodes": 0}, "settings.max_nodes"),
    ({"max_parallel": 100000}, "settings.max_parallel"),   # VAL-1: no upper bound → resource exhaustion
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


def test_launch_parallel_aliases_preserve_saved_file_explicit_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_EVAL_PARALLEL", "11")
    (tmp_path / "ui_settings.json").write_text(json.dumps({
        "eval_parallel": 10,
    }), encoding="utf-8")
    task_file = tmp_path / "parallel-task.json"
    task_file.write_text(json.dumps({
        "task": _toy(),
        "settings": {"eval_parallel": 7},
    }), encoding="utf-8")

    response = TestClient(make_app(tmp_path)).post("/api/start/preflight", json={
        "run_id": "parallel-precedence",
        "task_file": str(task_file),
        "settings": {"max_parallel": 2},
    })
    assert response.status_code == 200
    settings = response.json()["preview"]["settings"]
    assert settings["eval_parallel"] == 2     # explicit legacy beats file/saved/env canonical


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


def test_start_missing_task_required_key_refuses_before_popen_and_releases_namespace(
        tmp_path, monkeypatch):
    key = "START_RESEARCH_KEY"
    endpoint = "https://research.invalid/v1"
    monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv(f"{key}_BASE_URL", raising=False)
    spawned = []
    monkeypatch.setattr(
        "looplab.serve.routers.control._spawn_engine",
        lambda *args, **kwargs: spawned.append((args, kwargs)))
    client = TestClient(make_app(tmp_path))
    request = {
        "run_id": "missing-role-key",
        "task": _toy(),
        "settings": {
            "backend": "llm",
            "llm_profiles": {"research": {
                "model": "research", "base_url": endpoint, "api_key_env": key,
            }},
            "role_profiles": {"researcher": "research"},
        },
    }
    assert client.post("/api/start/preflight", json=request).status_code == 200

    response = client.post("/api/start", json=request)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "launch_credentials_invalid"
    assert spawned == []
    assert not (tmp_path / "missing-role-key").exists()


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


# --- C3: `task_file` allow-list ------------------------------------------------------------------
#
# `POST /api/start` took an arbitrary absolute path out of the request body and loaded it with no
# containment of any kind. The UI token does not fix this: it is a privilege bug reachable BY the
# authenticated operator's own session (and by any same-origin page on a shared JupyterHub, which
# `server.py` warns about at startup). These drive the boundary with real requests.

def test_task_file_outside_the_declared_roots_is_refused(tmp_path):
    """A readable, perfectly valid task JSON that simply lives somewhere the operator never declared."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    smuggled = outside / "task.json"
    smuggled.write_text(json.dumps(_toy()), encoding="utf-8")
    root = tmp_path / "runroot"
    root.mkdir()

    response = TestClient(make_app(root)).post("/api/start/preflight", json={
        "run_id": "smuggled", "task_file": str(smuggled),
    })

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "task_file_not_allowed"
    # The refusal names what IS allowed and never echoes the rejected path — that message is the
    # oracle the containment check exists to close.
    assert str(smuggled) not in json.dumps(detail)
    assert str(root) in detail["field_errors"]["task_file"]


def test_task_file_cannot_read_an_arbitrary_host_file(tmp_path):
    """The real shape: a file the server can read and the operator never declared runnable. The
    refusal must be the containment one, NOT a parser error — a parse failure that quotes its input
    is a content oracle."""
    root = tmp_path / "runroot"
    root.mkdir()
    for target in ("/etc/hostname", "/proc/self/environ"):
        response = TestClient(make_app(root)).post("/api/start/preflight", json={
            "run_id": "probe", "task_file": target,
        })
        assert response.status_code == 400, target
        assert response.json()["detail"]["code"] == "task_file_not_allowed", target


def test_task_file_symlink_out_of_a_declared_root_is_refused(tmp_path):
    """Resolve-then-contain: a symlink PLANTED inside an allowed root still resolves outside it."""
    root = tmp_path / "runroot"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    real = outside / "task.json"
    real.write_text(json.dumps(_toy()), encoding="utf-8")
    link = root / "innocent.json"
    link.symlink_to(real)

    response = TestClient(make_app(root)).post("/api/start/preflight", json={
        "run_id": "via-link", "task_file": str(link),
    })

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "task_file_not_allowed"


def test_task_file_envvar_expansion_cannot_echo_a_secret_back(tmp_path, monkeypatch):
    """`expandvars` runs on caller text, so `$SOME_KEY` becomes the secret's VALUE and the
    pre-existing `task_file_not_found` message echoed the resolved path verbatim. Containment runs
    FIRST, so no message that echoes a path is reachable for a path outside the roots."""
    monkeypatch.setenv("C3FIXTURE_API_KEY", "sk-abcdefABCDEF0123456789LEAK")
    root = tmp_path / "runroot"
    root.mkdir()

    response = TestClient(make_app(root)).post("/api/start/preflight", json={
        "run_id": "expand", "task_file": "$C3FIXTURE_API_KEY",
    })

    assert response.status_code == 400
    body = response.text
    assert "sk-abcdefABCDEF0123456789LEAK" not in body
    assert response.json()["detail"]["code"] == "task_file_not_allowed"


def test_task_file_inside_the_run_root_still_launches(tmp_path):
    """The allow-list must not break the shipped path: the run root is a declared root."""
    source = tmp_path / "ok-task.json"
    source.write_text(json.dumps(_toy()), encoding="utf-8")

    response = TestClient(make_app(tmp_path)).post("/api/start/preflight", json={
        "run_id": "allowed", "task_file": str(source),
    })

    assert response.status_code == 200, response.text
    assert response.json()["preview"]["source"] == "task_file"


def test_declared_tasks_dir_is_admitted_and_is_the_catalogue_list(tmp_path):
    """LOOPLAB_TASKS_DIR is how an operator declares another directory — and the SAME derivation
    feeds `GET /api/tasks`, so what the pick-list offers is exactly what the launcher accepts."""
    import os as _os
    from looplab.serve.launch import task_file_roots

    tasks_dir = tmp_path / "declared"
    tasks_dir.mkdir()
    source = tasks_dir / "declared-task.json"
    source.write_text(json.dumps(_toy()), encoding="utf-8")
    root = tmp_path / "runroot"
    root.mkdir()

    _os.environ["LOOPLAB_TASKS_DIR"] = str(tasks_dir)
    try:
        client = TestClient(make_app(root))
        response = client.post("/api/start/preflight", json={
            "run_id": "declared", "task_file": str(source),
        })
        assert response.status_code == 200, response.text
        offered = {t["path"] for t in client.get("/api/tasks").json()["tasks"]}
        assert str(source.resolve()) in offered
        # every path the catalogue offers is under a root the launcher admits — one derivation
        roots = task_file_roots(root)
        for path in offered:
            assert any(r == Path(path) or r in Path(path).parents for r in roots), path
    finally:
        _os.environ.pop("LOOPLAB_TASKS_DIR", None)
