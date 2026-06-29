"""JupyterHub / FUSE-mount compatibility regressions.

These guard the hardening that lets LoopLab launch in JupyterHub and survive an object-store FUSE
home (geesefs/s3fs): best-effort fsync (an unsupported-fs fsync must not abort a write), unique
atomic-write temps (two writers can't collide on a fixed `.tmp`), and the jupyter-server-proxy
launch spec.
"""
from __future__ import annotations

import os

import pytest

from looplab.atomicio import atomic_write_text, atomic_write_bytes, best_effort_fsync


def test_best_effort_fsync_swallows_unsupported(monkeypatch):
    """On a FUSE/S3 mount fsync can raise OSError (ENOTSUP/EINVAL/EIO) — that MUST be swallowed, else
    the per-event append (eventstore) and every snapshot write would abort the engine mid-run."""
    def _raise(_fd):
        raise OSError("fsync not supported on this fs")
    monkeypatch.setattr(os, "fsync", _raise)
    best_effort_fsync(0)            # must NOT raise
    # And it must not break a real atomic write either (the write reaches the OS buffer regardless).
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "x.json")
    atomic_write_text(p, '{"ok": true}')
    assert open(p, encoding="utf-8").read() == '{"ok": true}'


def test_atomic_write_uses_unique_temp_and_leaves_no_leftover(tmp_path):
    """atomic_write_bytes must use a UNIQUE temp (mkstemp), not a fixed `<name>.tmp` two concurrent
    writers would collide on, and must leave no stray temp behind after a successful write."""
    p = tmp_path / "data.json"
    atomic_write_bytes(p, b"first")
    atomic_write_bytes(p, b"second")
    assert p.read_bytes() == b"second"
    # No fixed-name temp and no leftover dot-temp files in the dir.
    assert not (tmp_path / "data.json.tmp").exists()
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == [], f"stray temp files left behind: {leftovers}"


def test_atomic_write_cleans_temp_on_failure(tmp_path, monkeypatch):
    """If os.replace fails (a FUSE rename hiccup), the temp must be cleaned up, not orphaned."""
    p = tmp_path / "data.json"
    def _boom(*a, **k):
        raise OSError("rename failed")
    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        atomic_write_bytes(p, b"x")
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == [], f"temp not cleaned on failure: {leftovers}"


def test_jupyter_serverproxy_spec_is_valid():
    """The jupyter-server-proxy entry point must return a launch spec jsp can use: a {port}-templated
    command that runs `looplab ui --no-build` with a pinned run-root, prefix-stripping (absolute_url
    False), and a Launcher tile."""
    from looplab.jupyter import setup_looplab
    spec = setup_looplab()
    assert spec["command"][:2] == ["looplab", "ui"]
    assert "{port}" in spec["command"]
    assert "--no-build" in spec["command"]            # never build on a noexec/FUSE home
    assert "--run-root" in spec["command"]
    assert spec["absolute_url"] is False              # jsp strips the prefix; backend sees /api/...
    assert spec["launcher_entry"]["title"] == "LoopLab"


def test_run_root_honors_env(monkeypatch):
    monkeypatch.setenv("LOOPLAB_RUN_ROOT", "/data/looplab")
    from looplab.jupyter import _run_root
    assert _run_root() == "/data/looplab"
