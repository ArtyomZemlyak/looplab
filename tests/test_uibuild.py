"""Unit tests for looplab.uibuild — the on-launch React-bundle auto-builder.

The npm calls are monkeypatched, so these run with no Node toolchain and never touch the real
ui/ tree (paths are pinned to tmp via LOOPLAB_UI_SRC / LOOPLAB_UI_DIST)."""
from __future__ import annotations

import errno
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

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
    uibuild._write_build_stamp(src / "dist", uibuild._build_digest(src))
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    called = []
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", lambda *a, **k: called.append(a) or True)

    assert uibuild.ensure_ui_built(log=lambda *_: None) is True
    assert called == []  # never shelled out to npm


def test_existing_bundle_without_freshness_stamp_rebuilds_by_default(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    _mark_built(src / "dist")
    (src / "node_modules").mkdir()
    dependency_digest = uibuild._dependency_digest(src)
    uibuild._dependency_stamp_path(src).write_text(dependency_digest + "\n", encoding="ascii")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    calls = []
    monkeypatch.setattr(
        uibuild, "_run", lambda args, *, cwd, log: calls.append(list(args)) or True)

    assert uibuild.ensure_ui_built(log=lambda *_: None) is True
    assert calls == [["npm", "run", "build"]]
    assert uibuild._installed_build_digest(src / "dist") == uibuild._build_digest(src)


def test_waiter_rechecks_freshness_inside_build_lock(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    calls = []
    lock_entries = []
    monkeypatch.setattr(
        uibuild, "_run", lambda args, *, cwd, log: calls.append(list(args)) or True)

    @contextmanager
    def first_process_finishes_while_waiting(locked_src):
        lock_entries.append(locked_src)
        _mark_built(src / "dist")
        uibuild._write_build_stamp(src / "dist", uibuild._build_digest(src))
        yield

    monkeypatch.setattr(uibuild, "_ui_build_lock", first_process_finishes_while_waiting)

    assert uibuild.ensure_ui_built(log=lambda *_: None) is True
    assert lock_entries == [src]
    assert calls == [], "the waiter must consume the first process's fresh stamp, not rebuild"


def test_build_lock_failure_is_fail_closed(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", lambda *_args, **_kwargs: pytest.fail("must not run npm"))
    logs = []

    @contextmanager
    def denied_lock(_src):
        raise OSError("lock denied")
        yield  # pragma: no cover - required to define a contextmanager generator

    monkeypatch.setattr(uibuild, "_ui_build_lock", denied_lock)

    assert uibuild.ensure_ui_built(log=logs.append) is False
    assert any("safely lock" in message.lower() for message in logs)


def test_windows_build_lock_wait_is_bounded(monkeypatch):
    def always_contended(_fd, _mode, _length):
        raise OSError(errno.EACCES, "busy")

    fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=always_contended)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    ticks = iter([10.0, 310.1])
    monkeypatch.setattr(uibuild.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(uibuild.time, "sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="waiting for the UI build lock"):
        uibuild._acquire_windows_lock(SimpleNamespace(fileno=lambda: 7))


def test_build_lock_capability_failure_is_fail_closed(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    logs = []

    @contextmanager
    def unsupported_lock(_src):
        raise NotImplementedError("locking API unavailable")
        yield  # pragma: no cover - required to define a contextmanager generator

    monkeypatch.setattr(uibuild, "_ui_build_lock", unsupported_lock)

    assert uibuild.ensure_ui_built(log=logs.append) is False
    assert any("locking api unavailable" in message.lower() for message in logs)


def test_source_change_invalidates_existing_bundle_stamp(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    (src / "src").mkdir()
    source = src / "src" / "main.jsx"
    source.write_text("export const version = 1\n", encoding="utf-8")
    _mark_built(src / "dist")
    uibuild._write_build_stamp(src / "dist", uibuild._build_digest(src))
    (src / "node_modules").mkdir()
    dependency_digest = uibuild._dependency_digest(src)
    uibuild._dependency_stamp_path(src).write_text(dependency_digest + "\n", encoding="ascii")
    source.write_text("export const version = 2\n", encoding="utf-8")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    calls = []
    monkeypatch.setattr(
        uibuild, "_run", lambda args, *, cwd, log: calls.append(list(args)) or True)

    assert uibuild.ensure_ui_built(log=lambda *_: None) is True
    assert calls == [["npm", "run", "build"]]
    assert uibuild._installed_build_digest(src / "dist") == uibuild._build_digest(src)


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
    # A matching manifest stamp proves node_modules is current, so force rebuilds without reinstalling.
    (src / "node_modules").mkdir()
    monkeypatch.setattr(uibuild, "_dependency_digest", lambda _src: "deps-v1")
    uibuild._dependency_stamp_path(src).write_text("deps-v1\n", encoding="ascii")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    calls = []
    monkeypatch.setattr(uibuild, "_run",
                        lambda args, *, cwd, log: calls.append(list(args)) or True)

    assert uibuild.ensure_ui_built(force=True, log=lambda *_: None) is True
    assert ["npm", "run", "build"] in calls
    assert ["npm", "ci"] not in calls  # matching stamp -> no reinstall


def test_force_reinstalls_when_dependency_manifests_changed(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    _mark_built(src / "dist")
    (src / "node_modules").mkdir()
    uibuild._dependency_stamp_path(src).write_text("deps-old\n", encoding="ascii")
    monkeypatch.setattr(uibuild, "_dependency_digest", lambda _src: "deps-new")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    calls = []
    monkeypatch.setattr(
        uibuild, "_run", lambda args, *, cwd, log: calls.append(list(args)) or True)

    assert uibuild.ensure_ui_built(force=True, log=lambda *_: None) is True
    assert calls == [["npm", "ci"], ["npm", "run", "build"]]
    assert uibuild._dependency_stamp_path(src).read_text(encoding="ascii").strip() == "deps-new"


def test_stale_dependency_install_failure_does_not_build_or_advance_stamp(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    _mark_built(src / "dist")
    (src / "node_modules").mkdir()
    stamp = uibuild._dependency_stamp_path(src)
    stamp.write_text("deps-old\n", encoding="ascii")
    monkeypatch.setattr(uibuild, "_dependency_digest", lambda _src: "deps-new")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    calls = []

    def failed_install(args, *, cwd, log):
        calls.append(list(args))
        return False

    monkeypatch.setattr(uibuild, "_run", failed_install)

    assert uibuild.ensure_ui_built(force=True, log=lambda *_: None) is False
    assert calls == [["npm", "ci"], ["npm", "install"]]
    assert stamp.read_text(encoding="ascii").strip() == "deps-old"


def test_failed_forced_build_does_not_report_stale_dist_as_success(tmp_path, monkeypatch):
    src = _src_with_sources(tmp_path)
    _mark_built(src / "dist")
    (src / "node_modules").mkdir()
    monkeypatch.setattr(uibuild, "_dependency_digest", lambda _src: "deps-v1")
    uibuild._dependency_stamp_path(src).write_text("deps-v1\n", encoding="ascii")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    calls = []
    logs = []

    def failed_build(args, *, cwd, log):
        calls.append(list(args))
        return list(args) != ["npm", "run", "build"]

    monkeypatch.setattr(uibuild, "_run", failed_build)

    assert uibuild.ensure_ui_built(force=True, log=logs.append) is False
    assert calls == [["npm", "run", "build"]]
    assert (src / "dist" / "index.html").is_file(), "fixture keeps the previous bundle in place"
    assert any("build failed" in message.lower() for message in logs)


def test_ui_command_refuses_stale_bundle_after_requested_build_failure(tmp_path, monkeypatch):
    from looplab.cli import app

    src = _src_with_sources(tmp_path)
    _mark_built(src / "dist")
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))
    monkeypatch.setattr(uibuild, "ensure_ui_built", lambda **_kwargs: False)

    result = CliRunner().invoke(app, ["ui", "--run-root", str(tmp_path / "runs")])

    assert result.exit_code == 1
    assert "refusing to serve a stale or partial bundle" in result.output
    assert "--no-build" in result.output


def test_ui_command_refuses_partial_bundle_left_by_failed_first_build(tmp_path, monkeypatch):
    from looplab.cli import app

    src = _src_with_sources(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(src))

    def failed_after_partial_output(**_kwargs):
        _mark_built(src / "dist")
        return False

    monkeypatch.setattr(uibuild, "ensure_ui_built", failed_after_partial_output)

    result = CliRunner().invoke(app, ["ui", "--run-root", str(tmp_path / "runs")])

    assert result.exit_code == 1
    assert "refusing to serve a stale or partial bundle" in result.output
    assert "--no-build" in result.output


def test_dependency_digest_tracks_package_and_lockfile_content(tmp_path):
    src = _src_with_sources(tmp_path)
    first = uibuild._dependency_digest(src)
    (src / "package.json").write_text('{"name":"looplab-ui","version":"2"}', encoding="utf-8")
    second = uibuild._dependency_digest(src)
    (src / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    third = uibuild._dependency_digest(src)

    assert len(first) == 64
    assert len({first, second, third}) == 3


def test_no_sources_degrades_gracefully(tmp_path, monkeypatch):
    # Point at an empty dir (no package.json) — e.g. a non-source install.
    monkeypatch.setenv("LOOPLAB_UI_SRC", str(tmp_path / "ui"))
    monkeypatch.setattr(uibuild, "_has_npm", lambda: True)
    monkeypatch.setattr(uibuild, "_run", lambda *a, **k: pytest.fail("nothing to build"))
    logs = []

    assert uibuild.ensure_ui_built(log=logs.append) is False
    assert any("cannot build" in m.lower() for m in logs)
