"""I3 sandbox behavior + I10 variance-gate unit tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from looplab.trust.gate import one_se_better
from looplab.runtime.sandbox import SubprocessSandbox, _json_line_extras, _parse_metric


def test_sandbox_captures_metric(tmp_path):
    sb = SubprocessSandbox()
    code = 'import json; print(json.dumps({"metric": 42.5}))'
    res = sb.run(code, str(tmp_path / "ok"), timeout=30.0)
    assert res.exit_code == 0
    assert res.metric == 42.5
    assert not res.timed_out


def test_parse_mem_bytes():
    from looplab.runtime.sandbox import parse_mem_bytes
    assert parse_mem_bytes("8g") == 8 * 1024**3
    assert parse_mem_bytes("512m") == 512 * 1024**2
    assert parse_mem_bytes("1024k") == 1024 * 1024
    assert parse_mem_bytes("2t") == 2 * 1024**4
    assert parse_mem_bytes("1073741824") == 1073741824
    assert parse_mem_bytes(4096) == 4096
    for off in ("", "  ", None, "0", "-1g", "garbage"):        # all disable the cap
        assert parse_mem_bytes(off) is None


def test_subprocess_mem_cap_applies_rlimit(tmp_path):
    """#5 host-OOM guard: with mem_bytes set, the eval child runs under an RLIMIT_AS soft cap, so a
    runaway allocation gets MemoryError instead of OOM-killing the host. POSIX-only (no rlimit on nt)."""
    resource = pytest.importorskip("resource")
    cap = 900 * 1024 * 1024
    sb = SubprocessSandbox(mem_bytes=cap)
    # the child reports the RLIMIT_AS it actually runs under -> proves the preexec_fn set it
    code = ('import resource, json;'
            ' print(json.dumps({"metric": resource.getrlimit(resource.RLIMIT_AS)[0]}))')
    res = sb.run(code, str(tmp_path / "capped"), timeout=30.0)
    assert res.exit_code == 0
    assert res.metric == cap                                    # soft limit == the requested cap

    # and an allocation past the cap fails rather than being served (would OOM the host uncapped)
    big = ('x = bytearray(4 * 1024 * 1024 * 1024)\nprint("ALLOCATED", len(x))')
    res2 = sb.run(big, str(tmp_path / "runaway"), timeout=30.0)
    assert res2.exit_code != 0 and "ALLOCATED" not in res2.stdout


def test_subprocess_mem_cap_off_by_default(tmp_path):
    # No cap configured -> the child's RLIMIT_AS is untouched (unlimited on a normal box), so the
    # default trusted-local tier is byte-for-byte unchanged (no regression for CUDA/torch evals).
    resource = pytest.importorskip("resource")
    sb = SubprocessSandbox()
    assert sb.mem_bytes is None
    code = ('import resource, json;'
            ' print(json.dumps({"metric": 1.0 if resource.getrlimit(resource.RLIMIT_AS)[0]'
            ' == resource.RLIM_INFINITY else 0.0}))')
    res = sb.run(code, str(tmp_path / "uncapped"), timeout=30.0)
    assert res.exit_code == 0 and res.metric == 1.0


def test_make_sandbox_wires_mem_local():
    from looplab.runtime.sandbox import make_sandbox
    s = make_sandbox("trusted_local", mem_local="8g", max_output_bytes=1000)
    assert isinstance(s, SubprocessSandbox) and s.mem_bytes == 8 * 1024**3
    s2 = make_sandbox("trusted_local")                         # default off
    assert s2.mem_bytes is None


def test_make_sandbox_wires_fsize_local():
    from looplab.runtime.sandbox import make_sandbox
    s = make_sandbox("trusted_local", fsize_local="2g")
    assert isinstance(s, SubprocessSandbox) and s.fsize_bytes == 2 * 1024**3
    assert make_sandbox("trusted_local").fsize_bytes is None    # default off


def test_subprocess_fsize_cap_bounds_a_runaway_write(tmp_path):
    """#P1-5 disk-fill guard: with fsize_bytes set, a child writing past the per-file cap gets SIGXFSZ
    instead of filling the disk; a small write under the cap succeeds. POSIX-only."""
    pytest.importorskip("resource")
    sb = SubprocessSandbox(fsize_bytes=1 * 1024 * 1024)         # 1 MiB per-file cap
    ok = sb.run('open("out.bin", "wb").write(b"x" * 1000)\nprint(\'{"metric": 1.0}\')',
                str(tmp_path / "small"), timeout=30.0)
    assert ok.exit_code == 0 and ok.metric == 1.0              # small write under the cap is fine
    big = sb.run('open("out.bin", "wb").write(b"x" * (8 * 1024 * 1024))\nprint("DONE")',
                 str(tmp_path / "big"), timeout=30.0)
    assert big.exit_code != 0 and "DONE" not in big.stdout     # 8 MiB > cap -> killed, never completes


def test_sandbox_relative_workdir(tmp_path, monkeypatch):
    """Regression: a relative workdir must not double against cwd."""
    monkeypatch.chdir(tmp_path)
    sb = SubprocessSandbox()
    res = sb.run('import json; print(json.dumps({"metric": 1.0}))',
                 "runs/x/nodes/node_0", timeout=30.0)
    assert res.exit_code == 0 and res.metric == 1.0


def test_sandbox_reports_failure_no_metric(tmp_path):
    sb = SubprocessSandbox()
    res = sb.run('print("no metric here")', str(tmp_path / "nometric"), timeout=30.0)
    assert res.metric is None  # -> orchestrator records node_failed


def test_sandbox_nonzero_exit(tmp_path):
    sb = SubprocessSandbox()
    res = sb.run('raise SystemExit(3)', str(tmp_path / "boom"), timeout=30.0)
    assert res.exit_code == 3


def test_sandbox_timeout_is_killed(tmp_path):
    sb = SubprocessSandbox()
    res = sb.run('import time; time.sleep(30)', str(tmp_path / "slow"), timeout=1.0)
    assert res.timed_out
    assert res.metric is None


@pytest.mark.parametrize(
    "cand,inc,std,n,direction,expected",
    [
        (1.0, 5.0, 2.0, 4, "min", True),    # clearly better (>1 SE)
        (4.9, 5.0, 2.0, 4, "min", False),   # within noise -> rejected
        (9.0, 5.0, 2.0, 4, "max", True),    # clearly better for max
        (3.0, 5.0, 0.0, 1, "min", True),    # no variance info -> strict compare
    ],
)
def test_one_se_gate(cand, inc, std, n, direction, expected):
    assert one_se_better(cand, inc, std, n, direction) is expected


# --- sandbox metric parsing: NaN/inf rejected, byte cap honored, tier kwargs tolerated ------------

def test_inf_metric_rejected_in_solution_path():
    assert _parse_metric('{"metric": Infinity}') is None
    assert _parse_metric('{"metric": 1.5}') == 1.5


def test_make_sandbox_tolerates_extra_kwargs():
    from looplab.runtime.sandbox import SubprocessSandbox, make_sandbox
    s = make_sandbox("trusted_local", image="ignored", max_output_bytes=1000)
    assert isinstance(s, SubprocessSandbox) and s.max_output_bytes == 1000


def test_json_line_extras_rejects_nan_and_inf():
    out = '{"metric": 0.5, "loss": NaN, "lr": Infinity, "recall": 0.7}'
    extras = _json_line_extras(out)
    assert extras == {"recall": 0.7}


def test_clamp_tail_bytes_respects_byte_budget_on_multibyte():
    from looplab.runtime.sandbox import _clamp_tail_bytes
    s = "世" * 100                                              # 300 UTF-8 bytes
    out = _clamp_tail_bytes(s, 90)
    assert len(out.encode("utf-8")) <= 90                       # a plain [-90:] would keep 270 bytes


@pytest.mark.parametrize("argv", [[], ["python", "bad\x00arg"]])
def test_run_argv_reports_malformed_argv_as_launch_failure(tmp_path, argv):
    """An agent-authored empty/NUL argv fails the node; it must not crash the engine process."""
    from looplab.runtime.sandbox import run_argv

    rc, out, err, timed_out = run_argv(argv, str(tmp_path), timeout=1)
    assert rc == -1 and out == "" and not timed_out
    assert "failed to launch" in err


def test_tee_drain_reads_newline_free_output_in_bounded_chunks():
    """A giant candidate-controlled line must never reach an unbounded readline() allocation."""
    from looplab.runtime.sandbox import _tee_drain

    class GuardedStream:
        def __init__(self, payload: bytes):
            self.payload = payload
            self.pos = 0
            self.requests = []

        def read1(self, size: int) -> bytes:
            self.requests.append(size)
            chunk = self.payload[self.pos:self.pos + size]
            self.pos += len(chunk)
            return chunk

        def readline(self, *_args, **_kwargs):
            raise AssertionError("newline-free output must not use unbounded readline()")

        def close(self):
            pass

    class Proc:
        def __init__(self):
            self.stdout = GuardedStream(b"x" * 1_000_000)  # deliberately no newline
            self.stderr = GuardedStream(b"")
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    proc = Proc()
    rc, out, err, timed_out = _tee_drain(proc, None, 10.0, 1024, None)
    assert rc == 0 and not timed_out and err == ""
    assert proc.stdout.requests and max(proc.stdout.requests) == 64 * 1024
    # Internal ring is allowed up to 2*max(4*requested, 256k); the public run_argv applies the
    # tighter requested cap afterward. Most importantly, it never retains the whole 1 MB line.
    assert 0 < len(out.encode("utf-8")) <= 512_000


def test_run_argv_force_removes_daemon_container_after_cancel(tmp_path, monkeypatch):
    """Killing `docker run` must also kill the daemon-owned container identified by --cidfile."""
    import io
    import subprocess
    import threading

    import looplab.runtime.sandbox as sb

    cid = "a" * 64
    seen = {"argv": None, "cleanup": None}

    class Proc:
        def __init__(self, argv, **_kwargs):
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")
            self.returncode = None
            seen["argv"] = list(argv)
            cpath = Path(argv[argv.index("--cidfile") + 1])
            cpath.write_text(cid, encoding="ascii")

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("docker", timeout)
            return self.returncode

    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(sb.subprocess, "Popen", Proc)
    monkeypatch.setattr(sb, "_kill_tree", lambda proc: setattr(proc, "returncode", -9))

    def fake_run(argv, **_kwargs):
        seen["cleanup"] = list(argv)
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    rc, _out, _err, timed_out = sb.run_argv(
        ["docker", "run", "--rm", "image", "python", "solution.py"],
        str(tmp_path), timeout=30, cancel=cancel,
    )

    assert rc == -9 and timed_out
    assert "--cidfile" in seen["argv"]
    assert seen["cleanup"] == ["docker", "rm", "-f", cid]
    assert not list(tmp_path.glob(".looplab-container-*.cid"))


def test_run_argv_force_removes_daemon_container_after_stall_or_diverge(tmp_path, monkeypatch):
    """A STALL/DIVERGE watchdog force-kills `docker run` but returns timed_out=False, so gating daemon-
    container removal on `timed_out` alone leaked the container (still running, holding its GPU) while
    the cidfile — the only cleanup handle — was unlinked. Removal must fire for EVERY parent-side kill,
    recovered from the watchdog sentinel appended to stderr."""
    import io
    import looplab.runtime.sandbox as sb

    cid = "b" * 64

    class Proc:
        def __init__(self, argv, **_kwargs):
            self.stdout, self.stderr, self.returncode = io.BytesIO(b""), io.BytesIO(b""), -9
            Path(argv[argv.index("--cidfile") + 1]).write_text(cid, encoding="ascii")

        def wait(self, timeout=None):
            return -9

    monkeypatch.setattr(sb.subprocess, "Popen", Proc)

    for marker in (sb.STALL_SENTINEL, sb.DIVERGED_SENTINEL):
        cleanup = {}
        # Simulate the watchdog kill: non-zero rc, the sentinel appended to stderr, timed_out=False.
        monkeypatch.setattr(sb, "_tee_drain",
                            lambda *a, _m=marker, **k: (-9, "", f"boom {_m} tail", False))
        monkeypatch.setattr(
            sb.subprocess, "run",
            lambda argv, **k: cleanup.setdefault("argv", list(argv))
            or type("Done", (), {"returncode": 0})())
        rc, _out, _err, timed_out = sb.run_argv(
            ["docker", "run", "--rm", "image", "python", "solution.py"], str(tmp_path), timeout=30)
        assert timed_out is False                                   # a stall/diverge is not a hard timeout…
        assert cleanup.get("argv") == ["docker", "rm", "-f", cid], marker   # …but the container is still rm -f'd
    assert not list(tmp_path.glob(".looplab-container-*.cid"))


def test_kill_tree_prefers_atomic_group_kill_over_racy_psutil_snapshot_on_posix(monkeypatch):
    # MED (fork-during-kill race): on POSIX _kill_tree must kill the whole process group in one atomic
    # syscall (killpg), NOT snapshot psutil `children()` then kill each — the snapshot races a late fork
    # (a DataLoader worker spawned after the snapshot escapes and keeps a GPU the scheduler then
    # releases). Verify the atomic path is primary and the racy per-child snapshot is never consulted.
    import os
    import sys
    import types
    if os.name == "nt":
        pytest.skip("POSIX process-group kill")
    import looplab.runtime.sandbox as sb

    seen = {"killpg": [], "children": 0}
    monkeypatch.setattr(sb.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(sb.os, "killpg", lambda pgid, sig: seen["killpg"].append((pgid, sig)))

    class _FakeProc:                          # stand in for psutil.Process so nothing real is killed
        def __init__(self, _pid):
            pass

        def children(self, recursive=False):
            seen["children"] += 1             # the RACY snapshot — must NOT be reached on the happy path
            return []

        def kill(self):
            pass

    # Inject a fake `psutil` so the racy branch is exercised even where the `[proc]` extra isn't
    # installed (as in this env): pre-fix `_kill_tree` imports it and snapshots children() without ever
    # calling killpg; post-fix killpg runs first and returns before psutil is imported.
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.Process = _FakeProc
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    class _P:
        pid = 4321

    sb._kill_tree(_P())
    assert seen["killpg"] == [(4321, 9)]      # one atomic SIGKILL to the whole group…
    assert seen["children"] == 0             # …and the racy per-child snapshot was never taken


def test_kill_tree_reaps_a_grandchild_process(tmp_path):
    # Behavioral guard: the group kill must reap the WHOLE tree, including a grandchild the eval forked
    # (the exact escapee the race would have leaked). Spawned as run_argv does — a new session so the
    # child leads its own group.
    import os
    import subprocess
    import sys
    import time
    if os.name == "nt":
        pytest.skip("POSIX liveness probe (os.kill(pid, 0))")
    from looplab.runtime.sandbox import _kill_tree

    code = ("import subprocess, sys, time\n"
            "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(g.pid, flush=True)\n"
            "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(tmp_path),
                            stdout=subprocess.PIPE, start_new_session=True)
    grandchild_pid = int(proc.stdout.readline().strip())
    _kill_tree(proc)
    gone = False
    for _ in range(50):
        try:
            os.kill(grandchild_pid, 0)        # raises OSError once the grandchild is reaped
            time.sleep(0.1)
        except OSError:
            gone = True
            break
    try:
        proc.wait(timeout=5)                  # reap the killed parent so it doesn't linger as a zombie
    except Exception:
        pass
    assert gone, f"grandchild {grandchild_pid} survived _kill_tree (process group not reaped)"


def test_run_argv_cidfile_lives_outside_the_bind_mounted_workdir(tmp_path, monkeypatch):
    """SECURITY (#5): the docker cidfile must NOT be created under the workdir, which DockerSandbox
    bind-mounts into the container as writable /work. If it lived there, untrusted root code could
    enumerate + overwrite it and redirect the post-timeout `docker rm -f <cid>` at another tenant's
    container. It must live in the host-only temp dir the container never sees."""
    import io
    import looplab.runtime.sandbox as sb

    seen = {"cidfile": None}

    class Proc:
        def __init__(self, argv, **_kwargs):
            self.stdout, self.stderr, self.returncode = io.BytesIO(b""), io.BytesIO(b""), 0
            seen["cidfile"] = Path(argv[argv.index("--cidfile") + 1])

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(sb.subprocess, "Popen", Proc)
    sb.run_argv(["docker", "run", "--rm", "image", "python", "solution.py"],
                str(tmp_path), timeout=30)

    cidfile = seen["cidfile"]
    assert cidfile is not None
    # Not under the bind-mounted workdir…
    assert tmp_path not in cidfile.parents
    # …and cleaned up afterwards (no host-temp leak).
    assert not cidfile.exists()


def test_run_argv_cleanup_swallows_a_cidfile_oserror(tmp_path, monkeypatch):
    """Robustness (#4): a cleanup OSError (e.g. the path turned into a directory, or a FUSE hiccup)
    must NOT turn a normal run into an engine-visible crash on the untrusted eval path."""
    import io
    import looplab.runtime.sandbox as sb

    class Proc:
        def __init__(self, argv, **_kwargs):
            self.stdout, self.stderr, self.returncode = io.BytesIO(b""), io.BytesIO(b""), 0
            cpath = Path(argv[argv.index("--cidfile") + 1])
            cpath.mkdir(parents=True, exist_ok=True)   # unlink() on a dir raises IsADirectoryError

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(sb.subprocess, "Popen", Proc)
    # Must not raise.
    rc, _out, _err, _timed = sb.run_argv(
        ["docker", "run", "--rm", "image", "python", "solution.py"], str(tmp_path), timeout=30)
    assert rc == 0
