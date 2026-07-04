"""Unit tests for looplab.uibuild — the on-launch React-bundle auto-builder.

The npm calls are monkeypatched, so these run with no Node toolchain and never touch the real
ui/ tree (paths are pinned to tmp via LOOPLAB_UI_SRC / LOOPLAB_UI_DIST)."""
from __future__ import annotations

import pytest

from looplab.serve import uibuild


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LOOPLAB_UI_SRC", raising=False)
    monkeypatch.delenv("LOOPLAB_UI_DIST", raising=False)


def _src_with_sources(tmp_path, *, lockfile=True):
    src = tmp_path / "ui"
    src.mkdir()
    (src / "package.json").write_text('{"name": "looplab-ui"}', encoding="utf-8")
    if lockfile:
        (src / "package-lock.json").write_text("{}", encoding="utf-8")
    return src


def _mark_built(dist):
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")


# ----------------------------------------------------------------- path resolution


def test_paths_default_relative_to_repo():
    # dist defaults to <source>/dist; source defaults to <repo>/ui.
    assert uibuild.ui_dist_dir() == uibuild.ui_source_dir() / "dist"
    assert uibuild.ui_source_dir().name == "ui"


def test_paths_honor_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(tmp_path / "frontend"))
    monkeypatch.setenv("LOOPLAB_UI_DIST", str(tmp_path / "elsewhere"))
    assert uibuild.ui_source_dir() == tmp_path / "frontend"
    assert uibuild.ui_dist_dir() == tmp_path / "elsewhere"


def test_is_built_tracks_index_html(tmp_path):
    dist = tmp_path / "dist"
    assert uibuild.is_built(dist) is False
    _mark_built(dist)
    assert uibuild.is_built(dist) is True


# ----------------------------------------------------------------- ensure_ui_built


def test_already_built_is_a_noop(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    _mark_built(src / "dist")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    called = []
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", lambda *a, **k: called.append(a) or True)

    assert uibuild.ensure_ui_built(log=lambda *_: None) is True
    assert called == []  # never shelled out to npm


def test_missing_npm_degrades_gracefully(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: False)
    monkeypatch.setattr(uibuild, "_run", lambda *a, **k: pytest.fail("must not run npm"))
    logs = []

    assert uibuild.ensure_ui_built(log=logs.append) is False
    assert any("npm" in m.lower() for m in logs)  # told the user how to build


def test_pinned_dist_is_never_built(tmp_path, monkeypatch):
    # LOOPLAB_UI_DIST means "use this prebuilt bundle" — never try to build into it.
    _src_with_sources(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(tmp_path / "ui"))
    monkeypatch.setenv("LOOPLAB_UI_DIST", str(tmp_path / "pinned"))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", lambda *a, **k: pytest.fail("must not build a pinned dist"))

    assert uibuild.ensure_ui_built(log=lambda *_: None) is False


def test_builds_when_missing(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path, lockfile=True)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    calls = []

    def fake_run(args, *, cwd, log):
        calls.append(list(args))
        if list(args) == ["npm", "run", "build"]:
            _mark_built(src / "dist")  # simulate vite emitting the bundle
        return True

    monkeypatch.setattr(uibuild, "_run", fake_run)

    assert uibuild.ensure_ui_built(log=lambda *_: None) is True
    assert ["npm", "ci"] in calls          # lockfile present -> reproducible install
    assert ["npm", "run", "build"] in calls
    assert uibuild.is_built(src / "dist")


def test_no_lockfile_uses_npm_install(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path, lockfile=False)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    calls = []

    def fake_run(args, *, cwd, log):
        calls.append(list(args))
        if list(args) == ["npm", "run", "build"]:
            _mark_built(src / "dist")
        return True

    monkeypatch.setattr(uibuild, "_run", fake_run)

    assert uibuild.ensure_ui_built(log=lambda *_: None) is True
    assert ["npm", "install"] in calls
    assert ["npm", "ci"] not in calls


def test_force_rebuilds_even_when_present(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    _mark_built(src / "dist")
    # node_modules already present so a forced rebuild skips install and only builds.
    (src / "node_modules").mkdir()
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    calls = []
    monkeypatch.setattr(uibuild, "_run",
                        lambda args, *, cwd, log: calls.append(list(args)) or True)

    assert uibuild.ensure_ui_built(force=True, log=lambda *_: None) is True
    assert ["npm", "run", "build"] in calls
    assert ["npm", "ci"] not in calls  # node_modules present -> no reinstall


def test_no_sources_degrades_gracefully(tmp_path, monkeypatch):
    # Point at an empty dir (no package.json) — e.g. a non-source install.
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(tmp_path / "ui"))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", lambda *a, **k: pytest.fail("nothing to build"))
    logs = []

    assert uibuild.ensure_ui_built(log=logs.append) is False
    assert any("cannot build" in m.lower() for m in logs)
