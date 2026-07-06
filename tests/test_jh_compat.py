"""JupyterHub / FUSE-mount compatibility regressions.

These guard the hardening that lets LoopLab launch in JupyterHub and survive an object-store FUSE
home (geesefs/s3fs): best-effort fsync (an unsupported-fs fsync must not abort a write), unique
atomic-write temps (two writers can't collide on a fixed `.tmp`), and the jupyter-server-proxy
launch spec.
"""
from __future__ import annotations

import os

import pytest

from looplab.core.atomicio import atomic_write_text, atomic_write_bytes, best_effort_fsync


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


def test_fsync_timeout_env_parse_tolerates_garbage(monkeypatch):
    """LOOPLAB_FSYNC_TIMEOUT is read at import; atomicio is imported transitively everywhere, so a
    garbage override (LOOPLAB_FSYNC_TIMEOUT=abc) must degrade to the default, not crash the app at
    load. A valid override is still honored. (`_fsync_timeout` reads the env live, so no reload.)"""
    import looplab.core.atomicio as aio
    assert aio._fsync_timeout() == 5.0              # default when unset
    monkeypatch.setenv("LOOPLAB_FSYNC_TIMEOUT", "abc")
    assert aio._fsync_timeout() == 5.0              # garbage -> default, no ValueError at import
    monkeypatch.setenv("LOOPLAB_FSYNC_TIMEOUT", "12.5")
    assert aio._fsync_timeout() == 12.5             # valid -> honored


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
    from looplab.runtime.jupyter import setup_looplab
    spec = setup_looplab()
    assert spec["command"][:2] == ["looplab", "ui"]
    assert "{port}" in spec["command"]
    assert "--no-build" in spec["command"]            # never build on a noexec/FUSE home
    assert "--run-root" in spec["command"]
    assert spec["absolute_url"] is False              # jsp strips the prefix; backend sees /api/...
    assert spec["launcher_entry"]["title"] == "LoopLab"


def test_run_root_honors_env(monkeypatch):
    monkeypatch.setenv("LOOPLAB_RUN_ROOT", "/data/looplab")
    from looplab.runtime.jupyter import _run_root
    assert _run_root() == "/data/looplab"


def test_oom_kill_classified_as_repairable_oom():
    """A pod cgroup-memory OOM-kill (SIGKILL → exit -9/137, no Python traceback) must classify as a
    distinct, REPAIRABLE 'oom' — not a generic 'crash' that the rule path abandons. An ordinary
    nonzero exit WITH a traceback stays 'crash'."""
    from types import SimpleNamespace
    from looplab.engine.orchestrator import _failure_reason, _rule_triage

    def res(exit_code, stderr, timed_out=False):
        return SimpleNamespace(drift=None, timed_out=timed_out, stderr=stderr, exit_code=exit_code)

    assert _failure_reason(res(-9, "")) == "oom"          # POSIX SIGKILL, no traceback
    assert _failure_reason(res(137, "Killed")) == "oom"   # 128+9, kernel "Killed" line, no traceback
    assert _failure_reason(res(-9, "Traceback (most recent call last):\n...")) == "crash"  # real crash
    assert _failure_reason(res(1, "ValueError: x")) == "crash"
    # a timeout-kill is also SIGKILL but is caught earlier as 'timeout', never 'oom'
    assert _failure_reason(res(-9, "", timed_out=True)) == "timeout"
    # and 'oom' is triaged as a repair (reduce memory), like 'timeout'
    assert _rule_triage("oom", "", attempt=1, max_attempts=1)["action"] == "repair"


def test_deps_install_stops_after_repeated_egress_timeouts(monkeypatch):
    """On a no-egress pod pip times out on EVERY missing lib; after a few CONSECUTIVE timeouts install()
    must SHORT-CIRCUIT rather than hang the full timeout × N. A single transient timeout must NOT
    disable self-prep, and any pip RESPONSE resets the latch."""
    import subprocess
    import looplab.runtime.deps as deps
    monkeypatch.setattr(deps, "_consecutive_install_timeouts", 0)  # isolate from other tests

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pip", timeout=1)
    monkeypatch.setattr(subprocess, "run", _timeout)

    # The first LATCH attempts actually run and time out (no short-circuit yet) — a single timeout
    # must not have latched.
    for _ in range(deps._EGRESS_TIMEOUT_LATCH):
        r = deps.install("torch", timeout=1)
        assert r.ok is False and r.timed_out is True
    # Latch tripped: a further install short-circuits WITHOUT calling subprocess again.
    r = deps.install("xgboost", timeout=1)
    assert r.ok is False and "skipped" in r.output

    # A pip RESPONSE (here a clean "no matching distribution" — egress works) resets the counter, so a
    # transient blip can't disable self-prep for the rest of the run.
    monkeypatch.setattr(deps, "_consecutive_install_timeouts", 1)

    class _P:
        returncode = 1
        stdout = "ERROR: No matching distribution found"
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    deps.install("nope", timeout=1)
    assert deps._consecutive_install_timeouts == 0


@pytest.mark.skipif(not hasattr(os, "sched_getaffinity"), reason="sched_getaffinity is POSIX/Linux only")
def test_sandbox_caps_blas_threads_to_cpu_quota(tmp_path):
    """On Linux the sandbox must bound BLAS/OpenMP thread pools to the CPU quota so one eval can't
    oversubscribe a cgroup-limited pod. We assert the env reaches the child by having it echo the var."""
    from looplab.runtime.sandbox import _run_argv
    import sys
    code = "import os; print(os.environ.get('OMP_NUM_THREADS', 'UNSET'))"
    exit_code, out, err, timed_out = _run_argv([sys.executable, "-c", code], tmp_path, timeout=30)
    assert exit_code == 0, err
    assert out.strip() == str(len(os.sched_getaffinity(0)))


def test_kill_process_tree_is_pid_recycle_safe_on_bogus_pid():
    """_kill_process_tree must never raise — and must refuse to signal a pid that isn't a looplab
    engine (PID-recycle guard). A almost-certainly-dead/foreign pid is a safe smoke test."""
    from looplab.serve.server import _kill_process_tree
    _kill_process_tree(999999)   # nonexistent pid -> no-op, no raise


def test_oom_repair_directive_says_reduce_memory(tmp_path):
    """The OOM repair must hand the LLM a MEMORY-reduction directive (the whole point of the 'oom'
    reason). Before the fix it fell to the generic 'diagnose the root cause' text — useless when the
    OOM-kill left no traceback — so repairs re-OOM'd. Distinct from the timeout (compute) directive."""
    from pathlib import Path
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.adapters.toytask import ToyTask
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    r, d = task.build_roles()
    eng = Engine(tmp_path / "demo", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4))
    oom = eng._repair_error_context("oom", "")
    assert "[failure kind: oom]" in oom
    assert "memory" in oom.lower() and "batch" in oom.lower()      # actionable memory-reduction
    timeout = eng._repair_error_context("timeout", "")
    assert "memory" not in timeout.lower()                          # the two directives stay distinct
