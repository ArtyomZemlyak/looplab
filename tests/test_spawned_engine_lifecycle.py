"""The engines THIS server spawned: may their spawn claim say "gone" once they exit, and who reaps
them (review 2026-09-22, SRV1-01).

THE DEFECT, reproduced end to end below: without `psutil` — the default `[ui]` install, which keeps
it optional on purpose — `_spawn_engine` kept only the child's PID and dropped the `Popen`, so
nothing in the server ever called `waitpid` on it. A detached engine that died BEFORE taking
`engine.lock` (here: a `resume` whose config snapshot is missing, which the CLI refuses at argument
validation) stayed a ZOMBIE, and the dependency-free liveness fallback `kill(pid, 0)` answers
"alive" for a zombie. Its spawn claim therefore never resolved: every command, start, Replay and
delete of that run refused `engine_start_uncertain`, and the operator's own escape hatch refused
too, with "The exact claimed child process is still alive" — until some unrelated `Popen` in the
same process happened to reap it through `subprocess._cleanup`.

Every test here waits for a child's exit with `os.waitid(..., WNOWAIT)`, which observes the exit
WITHOUT reaping it — so the zombie the defect needs is still there when the code under test looks.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")   # `serve/run_commands.py` speaks HTTPException

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve import engine_proc  # noqa: E402
from looplab.serve import run_commands as rc  # noqa: E402

_CAN_OBSERVE_EXIT_WITHOUT_REAPING = all(
    hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOWAIT", "WNOHANG"))
needs_waitid = pytest.mark.skipif(
    not _CAN_OBSERVE_EXIT_WITHOUT_REAPING,
    reason="observing an exit without reaping it needs POSIX waitid(WNOWAIT)")


@pytest.fixture
def no_psutil(monkeypatch):
    """The default `[ui]` install: `import psutil` raises ImportError."""
    monkeypatch.setitem(sys.modules, "psutil", None)


@pytest.fixture(autouse=True)
def _private_spawn_registry(monkeypatch):
    """Each test sees only the children it registers (the registry is process-global)."""
    monkeypatch.setattr(engine_proc, "_spawned_engines", {}, raising=False)


def _wait_for_exit_without_reaping(pid: int, *, timeout: float = 120.0):
    """Block until `pid` has exited, leaving it a zombie: `WNOWAIT` reports without consuming."""
    deadline = time.monotonic() + timeout
    while (result := os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)) is None:
        assert time.monotonic() < deadline, f"child {pid} did not exit within {timeout}s"
        time.sleep(0.02)
    return result


def _pre_lock_refusing_run(root: Path) -> tuple[Path, Path]:
    """A run whose `looplab resume` exits BEFORE `engine.lock`: the log exists, the config
    snapshot does not, and the CLI refuses that at argument validation rather than run on defaults."""
    rd = root / "wedge"
    rd.mkdir(parents=True)
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "wedge", "task_id": "t", "goal": "g", "direction": "min"})
    task = rd / "task.snapshot.json"
    task.write_text(json.dumps({"kind": "quadratic", "goal": "g", "direction": "min"}),
                    encoding="utf-8")
    assert not (rd / "config.snapshot.json").exists()
    return rd, task


@needs_waitid
@pytest.mark.parametrize("observer", ["recent_spawn_claim", "resolve_spawn_claim"])
def test_a_pre_lock_crash_resolves_its_spawn_claim_without_psutil(tmp_path, no_psutil, observer):
    """The reviewer's `e2e_wedge.py`, as a test: a REAL detached `resume` through `_spawn_engine`,
    claimed exactly the way `_spawn_under_claim` claims it, that refuses and exits pre-lock.

    Both readers of the claim must conclude the child is gone, and neither needs anything else to
    happen in the process first — no unrelated `Popen`, no psutil, no operator phrase."""
    root = tmp_path / "runs"
    rd, task = _pre_lock_refusing_run(root)
    svc = rc.RunCommandService(SimpleNamespace(root=root), command_timeout=2.0,
                               startup_timeout=0.5)
    command_id = "cmd_" + "0" * 32
    svc._record_spawn_claim(rd, command_id, None)            # the pre-Popen lease
    pid = engine_proc._spawn_engine(
        ["resume", str(rd), "--task-file", str(task)], run_dir=rd)
    assert isinstance(pid, int)
    assert isinstance(engine_proc._spawned_engines.get(pid), subprocess.Popen), (
        "the spawner keeps the Popen, the one handle that can both answer and reap")
    svc._record_spawn_claim(rd, command_id, pid)             # the persisted pid, as after Popen
    exit_status = _wait_for_exit_without_reaping(pid)
    # The premise: the CLI REFUSED (a usage error exits 2) and never reached the singleton lock.
    assert exit_status.si_code == os.CLD_EXITED and exit_status.si_status == 2, (
        (rd / "engine.stderr.log").read_text(encoding="utf-8")[-600:])
    assert not (rd / "engine.lock").exists()

    if observer == "recent_spawn_claim":
        deadline = time.monotonic() + 5.0
        while svc._recent_spawn_claim(rd):
            assert time.monotonic() < deadline, (
                "a child that exited before engine.lock still blocks every Popen of its run")
            time.sleep(0.05)
        assert not svc._spawn_claim_path(rd).exists(), "the dead child's claim is retired"
    else:
        # No confirmation phrase: a definitively dead child is not an operator judgement call.
        assert svc.resolve_spawn_claim(rd) == {
            "ok": True, "resolved": True, "reason": "child_definitively_gone"}
    assert pid not in engine_proc._spawned_engines, "the exited child was reaped and forgotten"


@needs_waitid
@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs a /proc filesystem")
def test_a_zombie_this_process_did_not_register_reads_as_dead_without_psutil(no_psutil):
    """The OS rung, for a child that did NOT come through `_spawn_engine` (so no `Popen` of ours
    can answer): `/proc/<pid>/stat` says `Z`, and that outranks `kill(pid, 0)`'s "it exists"."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        _wait_for_exit_without_reaping(child.pid)
        assert rc._process_alive(child.pid) is False
    finally:
        child.wait(timeout=10)


@needs_waitid
def test_a_registered_child_is_answered_by_its_own_popen_where_proc_is_unreadable(
        no_psutil, monkeypatch):
    """The registry rung stands on its own: with `/proc` unreadable (macOS, a hardened mount) and
    no psutil, `kill(pid, 0)` is all the OS rungs have left — and it says "exists" for a zombie. The
    `Popen` this process holds is asked FIRST, and asking REAPS the child."""
    monkeypatch.setattr(rc, "_proc_stat_state", lambda _pid: None)
    running = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                               stdin=subprocess.PIPE)
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        assert engine_proc._register_spawned_engine(running) == running.pid
        assert engine_proc._register_spawned_engine(finished) == finished.pid
        assert engine_proc.child_exited(running.pid) is False
        assert rc._process_alive(running.pid) is True

        _wait_for_exit_without_reaping(finished.pid)          # a zombie now, unreaped
        assert rc._process_alive(finished.pid) is False
        assert finished.returncode == 0, "the liveness question reaped it through Popen.poll"
        assert finished.pid not in engine_proc._spawned_engines
        # From here the pid is the kernel's again: not the registry's to answer.
        assert engine_proc.child_exited(finished.pid) is None

        running.stdin.close()                                 # let the other one finish too
        _wait_for_exit_without_reaping(running.pid)
        assert engine_proc.child_exited(running.pid) is True
        assert running.returncode == 0 and engine_proc._spawned_engines == {}
    finally:
        for child in (running, finished):
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


@needs_waitid
def test_the_reaper_signals_only_children_that_are_still_running(monkeypatch):
    """A reaped/exited child's pid belongs to whoever the kernel hands it to next; only an UNREAPED
    child's pid is still provably ours. So the reaper polls first and signals live children only."""
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/alice/")
    signalled = []
    monkeypatch.setattr(engine_proc, "_kill_process_tree", signalled.append)
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        engine_proc._register_spawned_engine(finished)
        engine_proc._register_spawned_engine(running)
        _wait_for_exit_without_reaping(finished.pid)
        engine_proc._reap_spawned_engines()
        assert signalled == [running.pid]
        assert finished.returncode is not None, "the exited child was reaped, never signalled"
        assert engine_proc._spawned_engines == {}
    finally:
        running.kill()
        running.wait(timeout=10)
        finished.wait(timeout=10)
