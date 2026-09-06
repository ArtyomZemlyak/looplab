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


# --- C3, second half: the name that was CHECKED must be the bytes that get PARSED ----------------
#
# Containment answers about a NAME. Three separate opens of that name used to follow the one check
# (`_require_task_file_size`'s stat, `load_document`'s read_text, `_source_fingerprint`'s
# read_bytes), so a file could pass containment as a plain task JSON and be something else by the
# time it was parsed — and the parser's error is embedded in the `400 invalid_task_file` message,
# i.e. exactly the content oracle containment exists to close. `read_confined_task_file` now opens
# ONCE, `O_NOFOLLOW` on the already-resolved path, and CASes the fstat against an lstat across the
# read. These drive that window from both sides; `_read_bounded` is the seam the swap happens in.

_SECRET_LINE = "root:$6$SUPERSECRETHASH:19000:0:99999:7:::"


def _swap_after(monkeypatch, replacement):
    """Run the real bounded read, then replace the file — the exact [fstat .. lstat] window."""
    from looplab.serve import launch

    real_read = launch._read_bounded
    state = {}

    def _hooked(fd):
        data = real_read(fd)
        replacement(state)
        return data

    monkeypatch.setattr(launch, "_read_bounded", _hooked)
    return state


def test_task_file_replaced_mid_read_is_refused_and_never_parsed(tmp_path, monkeypatch):
    """A DIFFERENT regular file swapped onto the validated name. `O_NOFOLLOW` cannot see this one —
    the identity CAS is what does, and the refusal must be the fail-closed set, never a 200."""
    root = tmp_path / "runroot"
    root.mkdir()
    source = root / "innocent.json"
    source.write_text(json.dumps(_toy()), encoding="utf-8")
    imposter = tmp_path / "imposter.json"
    imposter.write_text(json.dumps({**_toy(), "goal": _SECRET_LINE}), encoding="utf-8")

    import os as _os
    _swap_after(monkeypatch, lambda _s: _os.replace(imposter, source))

    response = TestClient(make_app(root)).post("/api/start/preflight", json={
        "run_id": "swapped", "task_file": str(source),
    })

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "task_source_changed"
    assert _SECRET_LINE not in response.text


def test_task_file_that_becomes_a_symlink_after_containment_is_refused(tmp_path, monkeypatch):
    """The swap that reaches OUT of every declared root. Resolve-then-contain answered about the
    file that was there at check time; `O_NOFOLLOW` on the resolved path answers about the one that
    is there at OPEN time, and it can refuse no legitimate launch — a resolved path holds no
    symlinks by construction."""
    root = tmp_path / "runroot"
    root.mkdir()
    secret = tmp_path / "shadow"
    secret.write_text(_SECRET_LINE, encoding="utf-8")
    source = root / "innocent.json"
    source.write_text(json.dumps(_toy()), encoding="utf-8")

    from looplab.serve import launch

    real = launch._confine_task_file

    def _swapping(root_arg, expanded):
        path = real(root_arg, expanded)
        path.unlink()
        path.symlink_to(secret)
        return path

    monkeypatch.setattr(launch, "_confine_task_file", _swapping)

    response = TestClient(make_app(root)).post("/api/start/preflight", json={
        "run_id": "relinked", "task_file": str(source),
    })

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "task_file_not_found"
    assert _SECRET_LINE not in response.text


def test_task_file_fifo_in_a_declared_root_does_not_hang_the_worker(tmp_path):
    """A FIFO passes `Path.is_file()`-shaped intent checks in spirit but blocks `read_text` forever.
    The open is non-blocking and a non-regular file is refused, so this returns instead of wedging
    the preflight worker — the timeout below IS the assertion."""
    import os as _os

    root = tmp_path / "runroot"
    root.mkdir()
    fifo = root / "pipe.json"
    _os.mkfifo(fifo)

    response = TestClient(make_app(root)).post("/api/start/preflight", json={
        "run_id": "fifo", "task_file": str(fifo),
    })

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "task_file_not_found"


def test_confined_read_parses_exactly_what_it_fingerprints(tmp_path):
    """The positive control, and the parity one: the single read must produce the SAME document the
    shipped `appconfig.load_document` produces from that path, and the fingerprint must describe the
    bytes that were parsed rather than a second, later read of the name."""
    import hashlib

    from looplab.core.appconfig import load_document
    from looplab.serve.launch import read_confined_task_file

    root = tmp_path / "runroot"
    root.mkdir()
    source = root / "unified.json"
    source.write_text(json.dumps({"task": _toy(), "settings": {"max_nodes": 3}}), encoding="utf-8")

    confined = read_confined_task_file(root, str(source))

    assert confined.data == source.read_bytes()
    assert confined.document() == load_document(source)
    assert confined.fingerprint()["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert confined.fingerprint()["size"] == len(confined.data)


def test_confined_read_of_a_bom_yaml_task_file_matches_the_shipped_loader(tmp_path):
    """Read parity is not only about JSON: `load_document` picks the parser by SUFFIX and tolerates a
    BOM, and the launch path must not quietly become a stricter reader than the engine's."""
    from looplab.core.appconfig import load_document
    from looplab.serve.launch import read_confined_task_file

    root = tmp_path / "runroot"
    root.mkdir()
    source = root / "with-bom.json"
    source.write_bytes(b"\xef\xbb\xbf" + json.dumps(_toy()).encode("utf-8"))

    assert read_confined_task_file(root, str(source)).document() == load_document(source)


def test_task_file_larger_than_the_cap_is_refused_before_it_is_parsed(tmp_path):
    from looplab.serve import launch

    root = tmp_path / "runroot"
    root.mkdir()
    source = root / "huge.json"
    source.write_bytes(b"{" + b" " * (launch._MAX_TASK_FILE_BYTES + 16) + b"}")

    response = TestClient(make_app(root)).post("/api/start/preflight", json={
        "run_id": "huge", "task_file": str(source),
    })

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "task_file_too_large"


def test_genesis_card_cannot_read_a_task_file_outside_the_declared_roots(tmp_path):
    """The sibling hole: the genesis card takes `task_file` straight from the request body and read
    it with no containment and no size cap to decide a display-only backend hint. Same allow-list
    now; the card still renders (the hint is best-effort), it just no longer opens the file."""
    from looplab.serve.launch import _defaults_backend_llm

    root = tmp_path / "runroot"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    smuggled = outside / "repo-task.json"
    smuggled.write_text(json.dumps({
        "kind": "repo", "goal": "g", "direction": "max", "repo": str(tmp_path)}), encoding="utf-8")

    # Uncontained (the historical CLI-shaped call) the file is read and the hint fires...
    assert _defaults_backend_llm({}, str(smuggled), {}, {}) is True
    # ...and with the server's run root supplied, the same path is refused and no hint is derived.
    assert _defaults_backend_llm({}, str(smuggled), {}, {}, root) is False


def test_declared_tasks_dir_takes_a_list(tmp_path, monkeypatch):
    """`LOOPLAB_TASKS_DIR` is `os.pathsep`-separated. An operator with tasks in two places must not
    have to choose — the way out of that choice is pointing it at the common ancestor, which is how
    an allow-list ends up allowing a whole disk. Both declared dirs are admitted AND both are in the
    catalogue, because the two are one derivation."""
    import os as _os

    root = tmp_path / "runroot"
    root.mkdir()
    first, second = tmp_path / "team", tmp_path / "mine"
    for index, directory in enumerate((first, second)):
        directory.mkdir()
        (directory / f"task-{index}.json").write_text(json.dumps(_toy()), encoding="utf-8")
    monkeypatch.setenv("LOOPLAB_TASKS_DIR", _os.pathsep.join([str(first), str(second)]))

    client = TestClient(make_app(root))
    offered = {t["path"] for t in client.get("/api/tasks").json()["tasks"]}
    for index, directory in enumerate((first, second)):
        source = directory / f"task-{index}.json"
        response = client.post("/api/start/preflight", json={
            "run_id": f"declared-{index}", "task_file": str(source),
        })
        assert response.status_code == 200, response.text
        assert str(source.resolve()) in offered
    # A sibling of the declared dirs is still refused — the list declares them, not their parent.
    outside = tmp_path / "not-declared.json"
    outside.write_text(json.dumps(_toy()), encoding="utf-8")
    refusal = client.post("/api/start/preflight", json={
        "run_id": "sibling", "task_file": str(outside),
    })
    assert refusal.status_code == 400
    assert refusal.json()["detail"]["code"] == "task_file_not_allowed"


# ------------------------------------------------------------- /api/validate: the readiness QUESTION
# (doc 52 row 8). One rule of "is this launchable": the same `preflight_start` funnel `/api/start`
# refuses through, answered as a 200 verdict so a client can gate its launch button on the answer
# without carrying a copy of the rules — the TUI's `spec_ready` copy was deleted for it.

def test_validate_answers_ready_with_the_preflight_receipt_and_no_side_effects(tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    spawned = []
    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine",
                        lambda *args, **kwargs: spawned.append((args, kwargs)))
    before = sorted(path.name for path in tmp_path.iterdir())

    response = client.post("/api/validate", json={"run_id": "asked", "task": _toy(),
                                                  "settings": {"max_nodes": 4}})

    assert response.status_code == 200
    verdict = response.json()
    assert verdict["ready"] is True and verdict["ok"] is True
    assert len(verdict["validation_token"]) == 64
    assert verdict["preview"]["task"]["kind"] == "quadratic"
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert not (tmp_path / "asked").exists() and not spawned
    # The receipt is the SAME one preflight issues for the same proposal, so a launch bound to it
    # is bound to what was validated.
    preflight = client.post("/api/start/preflight", json={"run_id": "asked", "task": _toy(),
                                                          "settings": {"max_nodes": 4}})
    assert preflight.json()["validation_token"] == verdict["validation_token"]


@pytest.mark.parametrize("payload, status, code, field", [
    ({"run_id": "x", "task": {}}, 400, "invalid_task_source", "task"),
    ({"run_id": "", "task": {"benchmark": "quadratic"}}, 400, "invalid_run_id", "run_id"),
    ({"run_id": "x", "task": {"goal": "g"}}, 422, "invalid_task", "task"),
    ({"run_id": "x", "task": {"benchmark": "quadratic"}, "settings": {"max_nodes": "many"}},
     422, "invalid_launch_settings", "settings.max_nodes"),
])
def test_validate_answers_not_ready_with_the_refusal_instead_of_raising(tmp_path, payload, status,
                                                                        code, field):
    response = TestClient(make_app(tmp_path)).post("/api/validate", json=payload)
    assert response.status_code == 200
    verdict = response.json()
    assert verdict["ready"] is False
    assert verdict["status"] == status and verdict["code"] == code
    assert verdict["message"] and field in verdict["field_errors"]
    assert "validation_token" not in verdict


def test_validate_and_start_refuse_the_same_proposal_for_the_same_reason(tmp_path, monkeypatch):
    """THE PROPERTY: a client gating on the verdict can never be told "ready" about a proposal the
    launch refuses, nor "not ready" for a reason the launch would not give — the two are one call.
    Driven over the refusal ladder: a malformed source, an unsafe name, an adapter refusal (the
    repo rule `EvalSpec._command_or_stages` mirrors), unavailable paths, and a taken name."""
    client = TestClient(make_app(tmp_path))
    spawned = []
    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine",
                        lambda *args, **kwargs: spawned.append((args, kwargs)))
    (tmp_path / "taken").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    no_cmd = {"goal": "g", "direction": "max", "repo": str(repo)}
    empty_cmd = {"goal": "g", "direction": "max", "repo": str(repo), "cmd": {"metric": {"reader": "stdout_json", "key": "s"}}}
    proposals = [
        {"run_id": "x", "task": {}},
        {"run_id": "x", "task": _toy(), "task_file": "examples/toy_task.json"},
        {"run_id": "../x", "task": _toy()},
        {"run_id": "x", "task": {"kind": "no-such-kind"}},
        {"run_id": "x", "task": no_cmd},
        {"run_id": "x", "task": empty_cmd},
        {"run_id": "x", "task": _repo(tmp_path / "missing")},
        {"run_id": "taken", "task": _toy()},
    ]
    for proposal in proposals:
        verdict = client.post("/api/validate", json=proposal).json()
        start = client.post("/api/start", json=proposal)
        assert verdict["ready"] is False, proposal
        assert (verdict["status"], verdict["code"]) == (start.status_code, start.json()["detail"]["code"]), proposal
        assert verdict["message"] == start.json()["detail"]["message"], proposal
    assert not spawned
    # The rule the marker named — a repo task must carry a command or a stages pipeline, or onboard —
    # is stated by the adapters in the verdict: `validate_task`'s repo rule in the message, and
    # `EvalSpec._command_or_stages` (a pydantic refusal) in the field it names.
    stated = [client.post("/api/validate", json=p).json() for p in
              ({"run_id": "x", "task": no_cmd}, {"run_id": "x", "task": empty_cmd})]
    texts = [" ".join([v["message"], *v["field_errors"].values()]) for v in stated]
    assert all("cmd" in text or "command" in text for text in texts), stated
    assert any("stages" in text for text in texts), stated


def test_validate_rejects_a_non_json_body_like_its_siblings(tmp_path):
    client = TestClient(make_app(tmp_path))
    response = client.post("/api/validate", content=b"not json",
                           headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_launch_request"


def test_validate_does_not_answer_for_the_servers_own_condition(tmp_path, monkeypatch):
    """A 503 from the deletion-fence storage is not a fact about the proposal: it stays an error."""
    from looplab.serve import launch as launch_module
    from looplab.serve.launch import READINESS_REFUSALS

    def _broken(*args, **kwargs):
        from looplab.core.run_deletion import RunDeletionStorageError
        raise RunDeletionStorageError("fence store unreadable")

    monkeypatch.setattr(launch_module, "load_run_deletion_fence", _broken)
    response = TestClient(make_app(tmp_path)).post("/api/validate", json={"run_id": "x", "task": _toy()})
    assert response.status_code == 503 and 503 not in READINESS_REFUSALS
