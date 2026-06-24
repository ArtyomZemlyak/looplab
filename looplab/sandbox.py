"""Sandbox (I3, ADR-13). The tier is chosen by **trust mode**, not environment:

- ``trusted_local`` (default, the CLI): ``SubprocessSandbox`` — process isolation +
  resource limits (wall-clock timeout, process-tree kill, output caps, cwd scratch).
  The operator runs their own research on their own box, so the LLM-generated code is
  in the operator's trust domain. This is NOT a security boundary and none is claimed;
  none is needed. No Docker, no daemon — the whole engine + test suite run here.
- ``untrusted`` (hosted / web-UI / multi-tenant): ``DockerSandbox`` (--network none,
  cgroups, → gVisor) — a real boundary, required only when executing code on infra
  that must protect other users or the host.

`make_sandbox(trust_mode)` selects the tier; both satisfy the `Sandbox` Protocol, so
swapping subprocess→Docker is a config change, never a code change.

Reads the metric from the last stdout line that is JSON containing a "metric" key.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

# Env-var NAMES that look like a secret — redacted from the child process environment so generated
# code can't read (and persist into the event log) the operator's keys/tokens. Name-based, so it
# never touches PATH/SYSTEMROOT/TEMP etc. that a process legitimately needs.
_SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API_KEY)", re.IGNORECASE)


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    metric: Optional[float]
    timed_out: bool
    # Drift cross-check (RepoTask Phase 4, eval_trust_mode="ratify_freeze_drift"): set when an
    # independent reader's value diverged from the frozen adapter's. {"primary","cross",
    # "tolerance"}. When set, `metric` is forced to None (an uncorroborated metric is not
    # trusted) and the orchestrator records a `spec_drift` event. None on the normal path.
    drift: Optional[dict] = None
    # Multi-objective (#5, RepoTask): extra reported metrics {name: value} (audit) and unmet
    # hard constraints [{name,value,max,min}]. A node with violations stays measured but is
    # excluded from best-selection. None on the normal path.
    extra_metrics: Optional[dict] = None
    violations: Optional[list] = None


class Sandbox(Protocol):
    def run(self, code: str, workdir: str, timeout: float,
            env: Optional[dict] = None, cancel=None) -> RunResult: ...


def _to_float(v) -> Optional[float]:
    """Parse a metric value, rejecting non-finite (NaN/inf) — a diverged run reads as 'no
    metric', never slips into best-selection (where min/max over NaN is undefined)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _json_line_metric(text: str, key: str = "metric") -> Optional[float]:
    """Last stdout line that is a JSON object containing `key`. The one tolerant metric-line
    scanner — both the solution.py path (_parse_metric) and the command-eval readers use it."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and key in obj:
            return _to_float(obj[key])
    return None


def _parse_metric(stdout: str) -> Optional[float]:
    return _json_line_metric(stdout, "metric")


def _run_argv(argv: list[str], workdir: str, timeout: float,
              env: Optional[dict] = None, max_output_bytes: int = 64_000, cancel=None):
    """Run one subprocess (argv, no shell) in `workdir` with timeout + process-tree kill +
    capped UTF-8/replace capture. Returns (returncode, stdout, stderr, timed_out). The single
    place process management lives — SubprocessSandbox, DockerSandbox, and command_eval all
    route through it so timeouts/tree-kill/encoding behave identically everywhere.

    `cancel` (optional `threading.Event`): when set mid-run, tree-kill the subprocess and return
    early — this is how an operator's `node_abort` interrupts an in-flight eval (the engine watches
    the event log and sets it). When `cancel` is None the original single-`communicate` fast path
    runs unchanged. Repeated `communicate(timeout=)` after a TimeoutExpired is the documented,
    output-preserving way to poll, so capping a chatty run's pipe never deadlocks."""
    wd = Path(workdir).resolve()
    wd.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    # Don't hand the child code the host's secrets (review C2): a `print(os.environ)` or a stack
    # trace would otherwise exfiltrate LLM_API_KEY / cloud creds into the durable stdout tail. Drop
    # env vars whose NAME looks secret, but keep everything a process needs (PATH, SYSTEMROOT, …)
    # and always keep what the engine explicitly passes in `env` (e.g. LOOPLAB_EVAL_SEED).
    base = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    full_env = {**base, **{k: str(v) for k, v in (env or {}).items()}}
    # Run the child in UTF-8 mode so its `open()`/stdio default to UTF-8 even on Windows (whose
    # default is cp1252). LLM-written solutions and real benchmark data (mle-bench CSVs) are UTF-8 and
    # routinely crash with a cp1252 UnicodeDecodeError on the Windows host path. (The Docker/untrusted
    # tier runs a Linux image that already defaults to UTF-8, so this primarily fixes the host
    # SubprocessSandbox path.) setdefault: an explicit engine/env value still wins.
    full_env.setdefault("PYTHONUTF8", "1")
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.Popen(
            argv, cwd=str(wd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=full_env, **kwargs)
    except OSError as e:
        return -1, "", f"failed to launch: {e}", False
    timed_out = False
    if cancel is None:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            timed_out = True
    else:
        import time as _time
        deadline = _time.monotonic() + timeout
        while True:
            try:
                out, err = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if cancel.is_set() or _time.monotonic() >= deadline:
                    _kill_tree(proc)
                    out, err = proc.communicate()
                    timed_out = True
                    break
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, (out or "")[-max_output_bytes:], (err or "")[-max_output_bytes:], timed_out


def _kill_tree(proc: "subprocess.Popen") -> None:
    try:
        import psutil  # optional (extras: proc)

        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
        return
    except Exception:
        pass
    # Fallback (no psutil): OS-branched WHOLE-TREE kill. Plain proc.kill() on Windows ends only
    # the direct child, orphaning grandchildren (DataLoader/worker/nested-train subprocesses) —
    # so use `taskkill /T` to terminate the tree; on POSIX kill the process group.
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class SubprocessSandbox:
    """trusted_local tier. Resource limits enforced portably: wall-clock timeout +
    process-tree kill (here) and output-size caps (truncated). Hard memory/network
    limits require the Docker tier; on the trusted-local path the operator owns the
    box, so they are best-effort, not a boundary."""

    def __init__(self, python: Optional[str] = None, max_output_bytes: int = 64_000,
                 **_: object):  # ignore tier-specific kwargs (symmetry with DockerSandbox)
        self.python = python or sys.executable
        self.max_output_bytes = max_output_bytes

    def run(self, code: str, workdir: str, timeout: float = 30.0,
            env: Optional[dict] = None, cancel=None) -> RunResult:
        wd = Path(workdir).resolve()  # absolute -> safe regardless of caller's cwd
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "solution.py").write_text(code, encoding="utf-8")
        rc, out, err, to = _run_argv(
            [self.python, "solution.py"],  # by name, relative to cwd -> no path doubling
            str(wd), timeout, env, self.max_output_bytes, cancel)
        return RunResult(exit_code=rc, stdout=out, stderr=err,
                         metric=_parse_metric(out), timed_out=to)


class DockerSandbox:
    """untrusted tier (ADR-13). Real boundary: the solution runs inside ``docker run
    --network none`` with the scratch workdir bind-mounted at /work, so arbitrary code can't
    reach the network or the host FS outside the mount. Fails LOUDLY if the docker CLI is
    absent rather than silently degrading the boundary."""

    def __init__(self, image: str = "python:3.12-slim", network: str = "none",
                 max_output_bytes: int = 64_000, runtime: Optional[str] = None, **_: object):
        self.image = image
        self.network = network
        self.max_output_bytes = max_output_bytes
        # B4+ hostile tier: an OCI runtime that is a REAL isolation boundary for untrusted code —
        # gVisor ("runsc", user-space kernel) or Kata ("kata-runtime", microVM). None = the default
        # shared-kernel runtime (untrusted tier). Passed to `docker run --runtime`.
        self.runtime = runtime

    def run(self, code: str, workdir: str, timeout: float = 30.0,
            env: Optional[dict] = None, cancel=None) -> RunResult:
        import shutil as _sh
        if not _sh.which("docker"):
            raise RuntimeError(
                "trust_mode='untrusted' needs the docker CLI to sandbox the solution, but it "
                "was not found on PATH. Install Docker or use trust_mode='trusted_local'.")
        wd = Path(workdir).resolve()
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "solution.py").write_text(code, encoding="utf-8")
        envs = []
        for k, v in (env or {}).items():            # pass env into the container explicitly
            envs += ["-e", f"{k}={v}"]
        # In-container self-limit (coreutils `timeout`): killing the host `docker run` CLI on
        # timeout does NOT stop the daemon-owned container, so bound it from INSIDE — the
        # container exits at `timeout` and `--rm` removes it, so a runaway leaks at most ~timeout
        # seconds even if the host kills the client first. Host timeout gets a grace margin.
        secs = max(1, int(timeout))
        rt = ["--runtime", self.runtime] if self.runtime else []   # B4+ gVisor/Kata isolation tier
        argv = (["docker", "run", "--rm", "--network", self.network, *rt,
                 "--pids-limit", "1024",      # fork-bomb guard (review C1: no pids limit before)
                 "-v", f"{wd.as_posix()}:/work", "-w", "/work"] + envs
                + [self.image, "timeout", "-k", "5", str(secs), "python", "solution.py"])
        rc, out, err, to = _run_argv(argv, str(wd), timeout + 15.0, None, self.max_output_bytes, cancel)
        timed_out = to or rc == 124          # coreutils timeout exits 124 when it fires
        return RunResult(exit_code=rc, stdout=out, stderr=err,
                         metric=(None if timed_out else _parse_metric(out)), timed_out=timed_out)


def make_sandbox(trust_mode: str = "trusted_local", *, image: Optional[str] = None,
                 **kwargs) -> Sandbox:
    """Select the sandbox tier from the trust mode (ADR-13). `image` is routed only to the
    Docker tier (the subprocess tier ignores it)."""
    if trust_mode == "trusted_local":
        return SubprocessSandbox(**kwargs)
    if trust_mode == "untrusted":
        return DockerSandbox(image=image or "python:3.12-slim", **kwargs)
    if trust_mode == "hostile":
        # B4+ true-isolation tier: shared-kernel container hardening is NOT an isolation boundary for
        # untrusted LLM code; run under gVisor (runsc) by default. Override via `runtime`.
        kwargs.setdefault("runtime", "runsc")
        return DockerSandbox(image=image or "python:3.12-slim", **kwargs)
    raise ValueError(f"unknown trust_mode: {trust_mode!r}")
