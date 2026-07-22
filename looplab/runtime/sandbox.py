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
SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API_KEY)", re.IGNORECASE)

# A sane wall-clock ceiling for any single subprocess run. A "timeout" larger than this is a
# misconfiguration, not an intent, so it is clamped rather than trusted — one eval must not be able
# to wedge the loop for a week (or forever) on a fat-fingered/hostile value.
MAX_TIMEOUT_S = 24 * 3600.0    # 24 hours

_DOCKER_NVIDIA_RUNTIME_CACHE: Optional[bool] = None


def docker_nvidia_runtime_available() -> bool:
    """Whether this Docker daemon advertises the NVIDIA runtime, cached per process."""
    global _DOCKER_NVIDIA_RUNTIME_CACHE
    if _DOCKER_NVIDIA_RUNTIME_CACHE is not None:
        return _DOCKER_NVIDIA_RUNTIME_CACHE
    import shutil
    docker = shutil.which("docker")
    if not docker:
        _DOCKER_NVIDIA_RUNTIME_CACHE = False
        return False
    try:
        out = subprocess.run(
            [docker, "info", "--format", "{{json .Runtimes}}"],
            capture_output=True, text=True, timeout=5.0)
        _DOCKER_NVIDIA_RUNTIME_CACHE = (
            out.returncode == 0 and "nvidia" in (out.stdout or "").lower())
    except (OSError, subprocess.SubprocessError):
        _DOCKER_NVIDIA_RUNTIME_CACHE = False
    return _DOCKER_NVIDIA_RUNTIME_CACHE


class GpuPinUnenforceable(RuntimeError):
    """A reserved physical GPU pin cannot be enforced by the Docker daemon/runtime.

    Fail-closed is correct (never launch an unpinned container for a pinned node), but the eval path
    catches THIS type specifically to terminalize the ONE affected node as node_failed instead of
    letting a bare RuntimeError tear down the whole eval task group (cancelling in-flight siblings) and
    re-crash deterministically on every resume.
    """


def docker_gpu_argv(env: Optional[dict], *, runtime: Optional[str] = None) -> list[str]:
    """Translate a physical CUDA visibility pin to Docker's device request.

    A non-empty ``CUDA_VISIBLE_DEVICES`` is an explicit device fence, so inability to enforce it
    fails closed before container launch. Empty CPU masks and truly unspecified legacy environments
    need no Docker GPU capability and retain their historical no-argument behavior.
    """
    if not isinstance(env, dict) or "CUDA_VISIBLE_DEVICES" not in env:
        return []
    devices = str(env.get("CUDA_VISIBLE_DEVICES") or "").strip()
    if not devices or devices.lower() in {"-1", "none", "nodevfiles", "void"}:
        return []
    # Docker applies its NVIDIA device request before invoking the selected low-level runtime.
    # In particular gVisor's nvproxy path is driven by the same ``--gpus`` request, so a configured
    # ``runsc`` runtime is GPU-capable too.  If nvproxy/toolkit support is missing, container creation
    # itself fails closed; rejecting every hostile-tier GPU request here would also reject valid hosts.
    supported_runtime = runtime in (None, "", "runc", "nvidia", "runsc")
    if supported_runtime and docker_nvidia_runtime_available():
        return ["--gpus", f"device={devices}"]
    why = (f"the configured OCI runtime {runtime!r} cannot expose NVIDIA devices"
           if not supported_runtime else "the Docker daemon has no advertised NVIDIA runtime")
    raise GpuPinUnenforceable(
        f"GPU device pin {devices!r} was requested, but {why}; "
        "refusing to launch an unpinned container.")


def docker_gpu_env(env: Optional[dict], *, gpu_args: list[str]) -> dict:
    """Return the environment safe to forward into a Docker GPU/CPU container.

    ``CUDA_VISIBLE_DEVICES`` is a process-level CUDA selector, not a device-injection boundary.  A
    CUDA image commonly carries ``NVIDIA_VISIBLE_DEVICES=all`` and a daemon may use NVIDIA as its
    default runtime, so an explicit CPU reservation must override that image default with ``void``.
    Conversely, for a positive scheduler-owned pin, Docker's ``--gpus device=...`` request is the
    authoritative physical fence: do not forward a host ``NVIDIA_VISIBLE_DEVICES`` value that could
    widen/conflict with it, and do not re-forward the physical CUDA ordinal into the re-indexed child.
    Truly unspecified legacy environments retain their historical forwarding behavior.
    """
    # SECURITY: this is THE env boundary for the untrusted Docker tiers. `docker run` does not inherit
    # the host env, so only what BOTH callers (make_docker_wrap and DockerSandbox.run) forward via `-e`
    # from this dict reaches candidate code. Strip secret-named vars at this single choke point — the same
    # guard `run_argv` applies to os.environ — so a host LLM_API_KEY / cloud cred that rode in via `env`
    # is never handed to adversarial code, and neither caller has to re-implement the filter.
    clean = {k: v for k, v in env.items() if not SECRET_ENV.search(k)} if isinstance(env, dict) else {}
    if "CUDA_VISIBLE_DEVICES" not in clean:
        return clean
    devices = str(clean.get("CUDA_VISIBLE_DEVICES") or "").strip()
    if gpu_args:
        clean.pop("CUDA_VISIBLE_DEVICES", None)
        clean.pop("NVIDIA_VISIBLE_DEVICES", None)
    elif not devices or devices.lower() in {"-1", "none", "nodevfiles", "void"}:
        clean["NVIDIA_VISIBLE_DEVICES"] = "void"
    return clean


def finite_timeout(value, default: float = 600.0) -> float:
    """Coerce a timeout into a FINITE, BOUNDED number of seconds, capped at `MAX_TIMEOUT_S`.

    The fail-OPEN case is the only one that must be rewritten: a NaN/±inf deadline is NEVER reached,
    so `monotonic() >= start + timeout` stays False and a runaway never times out (arch-review §3
    P0-7 / §4 P1-5). NaN/inf/unparseable therefore fall back to `default`. Finite values are clamped
    to `[0, MAX_TIMEOUT_S]`: a negative deadline is already fail-SAFE (it fires immediately), and 0 is
    a deliberately-honored sentinel elsewhere (a profile can set timeout:0), so both stay non-fatal
    rather than being rewritten. Every subprocess deadline flows through `run_argv`, which applies
    this; the eval/stage builders apply it too so the bounded value is what gets traced. Note the
    stricter authoring gate in `validate_stages` still REJECTS a non-finite/non-positive stage timeout
    outright — this is the defensive back-stop for every other caller."""
    import math
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = float("nan")
    if not math.isfinite(v):
        try:
            v = float(default)
        except (TypeError, ValueError):
            v = 600.0
        if not math.isfinite(v):
            v = 600.0
    return max(0.0, min(v, MAX_TIMEOUT_S))


def docker_timed_out(rc: int) -> bool:
    """True when a coreutils-``timeout``-wrapped docker exit code means THIS run hit its wall-clock
    deadline. `timeout` exits 124 when SIGTERM stopped the process at the deadline, and 137 (128+9)
    when the process outlived the `-k 5` grace and needed the SIGKILL escalation — common for a tight
    BLAS/numpy loop that never hits a Python signal check. BOTH are a timeout, so flag both: otherwise
    the 137 falls through to `_failure_reason`'s OOM heuristic and a real timeout is mislabeled "oom"
    (wrong repair directive). A container OOM also exits 137, but in this `timeout -k` tier the
    escalation is the dominant 137 source and "reduce compute" is a fine response either way. This is
    the single home of the 124-vs-137 rule — the DockerSandbox and command_eval both use it."""
    return rc in (124, 137)


def _clamp_tail_bytes(s: str, max_bytes: int) -> str:
    """Keep the last `max_bytes` BYTES of `s` (the cap is named …_bytes). A plain `s[-n:]` slices by
    CHARACTER, so multibyte-heavy output (CJK ≈ 3 bytes/char) stores up to ~3× the intended byte
    budget in the durable per-node stdout tail. Encode, slice on the byte boundary, decode back
    (dropping a partial leading char)."""
    if not s:
        return s
    b = s.encode("utf-8", "replace")
    if len(b) <= max_bytes:
        return s
    return b[-max_bytes:].decode("utf-8", "ignore")


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
    # Intra-node sweep: when the solution ran a grid of configs in one process, it emits a final
    # `{"trials": [...]}` line; this carries that raw list of trial dicts. The orchestrator picks
    # the best feasible trial to set the node's scalar `metric`. None on the single-config path.
    trials: Optional[list] = None
    # Staged eval (multi-stage pipeline: data_prep → train → eval): per-stage outcome dicts
    # {name, status "ok"|"fail"|"timeout", exit_code, seconds}, in run order. `failed_stage` is the
    # name of the first stage that failed (None on full success). The last stage is the metric stage.
    # None on the classic single-command path.
    stages: Optional[list] = None
    failed_stage: Optional[str] = None
    # STALL watchdog: True when the stage was tree-killed for going silent while alive (a hung
    # distributed finalize / wedged CUDA op / deadlock), NOT for a real deadline timeout. A stall that
    # had already printed its metric keeps `metric` set — the orchestrator SALVAGES it (a completed
    # train+eval that only hung on teardown still counts) instead of wasting the whole run.
    stalled: bool = False


# Distinctive sentinel in the killed stage's stderr, so command_eval/the orchestrator can tell a STALL
# apart from any other non-zero exit without threading a new value through run_argv's 4-tuple return.
STALL_SENTINEL = "LOOPLAB health-check: stage STALLED"


class Sandbox(Protocol):
    def run(self, code: str, workdir: str, timeout: float,
            env: Optional[dict] = None, cancel=None) -> RunResult: ...


def _to_float(v) -> Optional[float]:
    """Parse a metric value, rejecting non-finite (NaN/inf) — a diverged run reads as 'no
    metric', never slips into best-selection (where min/max over NaN is undefined)."""
    from looplab.core.parse import to_float
    return to_float(v, finite=True)


def _last_json_dict(text: str, pred) -> Optional[dict]:
    """The LAST stdout line that parses as a JSON object satisfying `pred` — the one bottom-up
    tolerant scanner behind json_line_metric / json_line_extras / json_line_trials (and the
    mlebench grader), so trailing chatter after a solution's summary line is tolerated the same
    way everywhere. Returns the raw dict (callers extract what they need), or None."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and pred(obj):
            return obj
    return None


def json_line_metric(text: str, key: str = "metric") -> Optional[float]:
    """Last stdout line that is a JSON object containing `key`. The one tolerant metric-line
    scanner — both the solution.py path (_parse_metric) and the command-eval readers use it."""
    obj = _last_json_dict(text, lambda o: key in o)
    return None if obj is None else _to_float(obj[key])


def _parse_metric(stdout: str) -> Optional[float]:
    # Kept (not folded into its callers): the sandboxes' readable name for "read the solution's
    # self-reported metric", and imported directly by tests (test_sandbox_gate).
    return json_line_metric(stdout, "metric")


def json_line_extras(text: str, primary_key: str = "metric") -> dict:
    """Every OTHER numeric key on the SAME final JSON line that carries the metric — auto-captured as
    secondary metrics, so an experiment that prints {"metric": x, "recall@10": y, "mrr": z} surfaces
    ALL of them with no per-task config. Structural/bookkeeping keys and non-numeric values are skipped;
    only the primary key drives selection (extras are audit-only)."""
    _skip = {primary_key, "metric", "trials", "params", "seconds", "second", "time", "epoch", "step"}
    obj = _last_json_dict(text, lambda o: primary_key in o)
    if obj is None:
        return {}
    out = {}
    for k, v in obj.items():
        if k in _skip or isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            f = _to_float(v)     # same finiteness rule as the primary metric: JSON parses
            if f is not None:    # NaN/Infinity literals, and a NaN extra breaks serializers
                out[str(k)] = f
    return out


# Back-compat alias (pre-rename importers/tests use `_json_line_extras`).
_json_line_extras = json_line_extras


def json_line_trials(text: str) -> Optional[list]:
    """Last stdout line that is a JSON object with a "trials" key holding a list (intra-node
    sweep). Scans bottom-up like `json_line_metric`, so trailing chatter after the sweep's
    summary line is tolerated. Returns the raw list of trial dicts, or None if absent."""
    obj = _last_json_dict(text, lambda o: isinstance(o.get("trials"), list))
    return None if obj is None else obj["trials"]


# Back-compat alias (pre-rename importers/tests use `_json_line_trials`).
_json_line_trials = json_line_trials


def parse_mem_bytes(spec) -> Optional[int]:
    """Parse a human memory size ("8g", "512m", "1073741824", 4096) to a positive int byte count, or
    None for "" / 0 / an unparseable value (cap disabled). Suffixes k/m/g/t are powers of 1024, matching
    `docker run --memory`. Best-effort: a bad value silently disables the cap rather than crashing eval."""
    if spec is None:
        return None
    if isinstance(spec, (int, float)):
        n = int(spec)
        return n if n > 0 else None
    s = str(spec).strip().lower()
    if not s:
        return None
    mult = 1
    if s[-1] in "kmgt":
        mult = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[s[-1]]
        s = s[:-1].strip()
    try:
        n = int(float(s) * mult)
    except (ValueError, OverflowError):
        # OverflowError: `int(float("inf"))` / `int(float("1e400"))` — a non-finite operator value must
        # SILENTLY DISABLE the cap (as the docstring promises), not crash make_sandbox on engine setup.
        return None
    return n if n > 0 else None


def run_argv(argv: list[str], workdir: str, timeout: float,
             env: Optional[dict] = None, max_output_bytes: int = 64_000, cancel=None,
             log_path: Optional[str] = None, mem_bytes: Optional[int] = None,
             fsize_bytes: Optional[int] = None, health_check: bool = False,
             stall_timeout: Optional[float] = None):
    """Run one subprocess (argv, no shell) in `workdir` with timeout + process-tree kill +
    capped UTF-8/replace capture. Returns (returncode, stdout, stderr, timed_out). The single
    place process management lives — SubprocessSandbox, DockerSandbox, and command_eval all
    route through it so timeouts/tree-kill/encoding behave identically everywhere.

    ALL runs drain through the memory-bounded `_tee_drain` reader (there is no `communicate()`
    fast path — it buffered the ENTIRE child output in host RAM, a DoS on a chatty run; see the
    comment at the `_tee_drain` call). `cancel` and `log_path` are handled INSIDE that drain loop:

    `cancel` (optional `threading.Event`): when set mid-run, tree-kill the subprocess and return
    early — this is how an operator's `node_abort` interrupts an in-flight eval (the engine watches
    the event log and sets it); None simply never interrupts.

    `log_path` (optional): mirror the child's stdout+stderr to this file *live*, line by line, so a
    long eval (training epochs, tqdm) is tail-able in real time instead of opaque until it returns.
    The returned (capped) stdout/stderr are unchanged, so the metric reader + repair feedback see
    exactly what they did before. None simply keeps no live file (the drain still runs)."""
    # Bound the deadline at the universal choke point: a NaN/inf/negative timeout from ANY caller
    # would otherwise disable the wall-clock kill (a NaN deadline is never reached). See finite_timeout.
    timeout = finite_timeout(timeout)
    wd = Path(workdir).resolve()
    wd.mkdir(parents=True, exist_ok=True)
    argv = list(argv)
    docker_cidfile: Optional[Path] = None
    # Killing the local `docker run` CLI does NOT necessarily stop the daemon-owned container. Attach
    # a unique host cidfile at the universal argv choke point (covers DockerSandbox and command-eval),
    # then force-remove that exact container if host timeout/cancel kills the client first.
    if (len(argv) >= 2 and Path(str(argv[0])).stem.lower() in {"docker", "docker.exe"}
            and argv[1] == "run" and "--cidfile" not in argv):
        import tempfile
        import uuid
        # SECURITY: the cidfile MUST live outside the bind-mounted workdir. DockerSandbox mounts `wd`
        # into the container as writable /work (as root, no --user), so a cidfile under `wd` is
        # enumerable AND overwritable by untrusted solution code — which could then redirect the
        # post-timeout `docker rm -f <cid>` at a co-tenant container on a shared daemon (cross-tenant
        # DoS) or turn cleanup into an uncaught crash. Put it in the host-only temp dir the container
        # never sees; the random name doesn't pre-exist so docker's --cidfile (which refuses an
        # existing file) writes it, and we unlink it below.
        docker_cidfile = Path(tempfile.gettempdir()) / f".looplab-container-{uuid.uuid4().hex}.cid"
        argv[2:2] = ["--cidfile", str(docker_cidfile)]
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
        _mem = int(mem_bytes) if (mem_bytes and mem_bytes > 0) else None
        _fsize = int(fsize_bytes) if (fsize_bytes and fsize_bytes > 0) else None
        if _mem is not None or _fsize is not None:
            # Best-effort resource caps for the trusted_local tier (#5 / doc 17 §7.6, P1-5). RLIMIT_AS
            # bounds the child's VIRTUAL address space so a runaway trainer hits MemoryError instead of
            # OOM-killing the whole host (+ the engine); RLIMIT_FSIZE bounds the size of any single file
            # it writes so a runaway can't fill the disk (SIGXFSZ past the cap). preexec_fn runs in the
            # child AFTER fork, BEFORE exec — keep it tiny and swallow errors (an exception there aborts
            # the spawn). POSIX only; Windows has no rlimit (Job Objects would be the analog). Both cap
            # aggressively (AS is virtual, FSIZE is per-file), so each defaults OFF and the caller opts
            # in only where it fits (CUDA/torch reserve huge virtual; large checkpoints need big files).
            def _apply_rlimits(_m=_mem, _f=_fsize):
                import resource
                for _res, _val in ((resource.RLIMIT_AS, _m), (resource.RLIMIT_FSIZE, _f)):
                    if _val is None:
                        continue
                    try:
                        _soft, _hard = resource.getrlimit(_res)
                        _nh = _val if (_hard == resource.RLIM_INFINITY or _val < _hard) else _hard
                        resource.setrlimit(_res, (_val, _nh))
                    except (ValueError, OSError):
                        pass   # can't lower that limit (unprivileged / already lower) -> run uncapped
            kwargs["preexec_fn"] = _apply_rlimits
    # Don't hand the child code the host's secrets (review C2): a `print(os.environ)` or a stack
    # trace would otherwise exfiltrate LLM_API_KEY / cloud creds into the durable stdout tail. Drop
    # env vars whose NAME looks secret, but keep everything a process needs (PATH, SYSTEMROOT, …)
    # and always keep what the engine explicitly passes in `env` (e.g. LOOPLAB_EVAL_SEED).
    base = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    full_env = {**base, **{k: str(v) for k, v in (env or {}).items()}}
    # Run the child in UTF-8 mode so its `open()`/stdio default to UTF-8 even on Windows (whose
    # default is cp1252). LLM-written solutions and real benchmark data (mle-bench CSVs) are UTF-8 and
    # routinely crash with a cp1252 UnicodeDecodeError on the Windows host path. (The Docker/untrusted
    # tier runs a Linux image that already defaults to UTF-8, so this primarily fixes the host
    # SubprocessSandbox path.) setdefault: an explicit engine/env value still wins.
    full_env.setdefault("PYTHONUTF8", "1")
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    # Cap BLAS/OpenMP thread pools to the pod's CPU QUOTA, not the host core count. torch/numpy/sklearn
    # size their pools from os.cpu_count() (the HOST's cores), so one eval on a 4-vCPU JupyterHub pod
    # sharing a 64-core node would spin ~64 threads → context-switch thrash + CPU throttling billed to
    # the user. sched_getaffinity respects the cgroup cpuset where cpu_count does not; on an unconstrained
    # box it returns every core, so this setdefault equals the library default (no local regression).
    # setdefault: an explicit operator/engine value still wins. POSIX/Linux only (guarded).
    try:
        _aff = len(os.sched_getaffinity(0))         # type: ignore[attr-defined]
        _quota = str(_aff)
        # BLAS/OpenMP pools track the pod's CPU QUOTA (the cgroup cpuset), so an eval uses its cores
        # without oversubscribing a shared node. NUMEXPR is the exception: it HARD-rejects
        # NUMEXPR_NUM_THREADS > NUMEXPR_MAX_THREADS (default 64) with a loud "Error." line, so its two
        # vars are capped at 64 while the general BLAS/OpenMP vars get the full quota.
        _nx = str(min(_aff, 64))
        for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                     "VECLIB_MAXIMUM_THREADS"):
            full_env.setdefault(_var, _quota)
        for _var in ("NUMEXPR_NUM_THREADS", "NUMEXPR_MAX_THREADS"):
            full_env.setdefault(_var, _nx)
    except AttributeError:
        pass   # no sched_getaffinity (Windows/macOS) — leave the libraries' own defaults
    # Unbuffered child stdio so the live log (log_path) updates line-by-line rather than only when
    # the child's block buffer flushes. setdefault: an explicit value still wins. Harmless on the
    # buffered path (the parent reads pipes either way).
    if log_path:
        full_env.setdefault("PYTHONUNBUFFERED", "1")
    try:
        # Keep the pipes binary and decode only after the bounded drain.  TextIOWrapper.readline()
        # has no size limit: one candidate-controlled, newline-free stdout record can make it buffer
        # the whole record in host RAM before our tail cap gets a chance to run.  `_tee_drain` reads
        # fixed-size binary chunks instead, preserving the public str return values without that DoS.
        proc = subprocess.Popen(
            argv, cwd=str(wd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=full_env, **kwargs)
    except (OSError, ValueError, IndexError) as e:
        # ValueError: embedded NUL in an agent/operator-authored argv/env item.  IndexError: empty
        # argv.  Both are controlled launch failures, not reasons to crash the whole engine.
        if docker_cidfile is not None:
            docker_cidfile.unlink(missing_ok=True)
        return -1, "", f"failed to launch: {e}", False
    # ALWAYS drain through the memory-bounded reader (log_path=None keeps no file but still caps the
    # in-memory tail): `communicate()` buffered the ENTIRE stdout/stderr before clamping, so an
    # adversarial/buggy fast printer on the untrusted solution.py path (which never sets log_path)
    # could accumulate its whole output in HOST RAM for up to `timeout` seconds — a host-memory DoS.
    rc, out, err, timed_out = _tee_drain(proc, log_path, timeout, max_output_bytes, cancel,
                                         health_check=health_check, stall_timeout=stall_timeout)
    if docker_cidfile is not None:
        # Defense-in-depth: the cidfile now lives in the host temp dir (unreachable by the container),
        # but never let a cleanup hiccup (a FUSE OSError, or — pre-#5 — untrusted code having replaced
        # the path with a directory so unlink raises IsADirectoryError) turn a normal timeout into an
        # engine-visible crash on the untrusted eval path.
        try:
            # CODEX AGENT: divergence/stall watchdogs also force-kill the local docker client, but
            # deliberately return timed_out=False. That skips daemon-container removal, deletes the
            # only cidfile, and can release the GPU while the container keeps running. Track forced
            # termination separately and rm -f Docker containers for every parent-side kill reason.
            if timed_out:
                _remove_docker_container(str(argv[0]), docker_cidfile)
            docker_cidfile.unlink(missing_ok=True)
        except OSError:
            pass
    return rc, _clamp_tail_bytes(out, max_output_bytes), _clamp_tail_bytes(err, max_output_bytes), timed_out


# Back-compat alias: pre-rename importers use `_run_argv`, and tests monkeypatch THIS module
# attribute to stub process execution — so the sandboxes below call `_run_argv` (resolved at
# call time), keeping that seam intact.
_run_argv = run_argv


def _remove_docker_container(docker: str, cidfile: Path) -> None:
    """Best-effort cleanup after the docker CLI itself was killed. argv execution + strict CID shape
    avoid a shell/injection surface; `--rm` remains the normal successful-exit cleanup path."""
    try:
        cid = cidfile.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return
    if not re.fullmatch(r"[0-9a-fA-F]{12,64}", cid):
        return
    try:
        subprocess.run([docker, "rm", "-f", cid], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


class _StageHealthMonitor:
    """Detects a DEGENERATE (diverged) training stage from one streamed output channel: a non-finite
    loss or grad-norm (nan / inf) reported repeatedly, i.e. training that will never produce a useful model.

    Universal by design — a format-agnostic token scan for `loss`/`grad_norm` immediately followed by
    `nan`/`inf` — so it fires for HF Trainer, Lightning, or a hand-rolled loop with NO per-framework
    config and no Developer effort (the built-in half of the hybrid guard; the Developer is separately
    asked to add a fail-fast finite-loss assert). Deliberately STRICT (needs `threshold` such records) so
    an incidental `nan` token in a path/message can't trip it. Record-buffered: accepts both newline logs
    and carriage-return progress bars, never double-counting a chunk boundary.

    The count is CONSECUTIVE, not cumulative: a metric record reporting a *finite* loss/grad_norm resets
    it to zero, so the monitor fires only on SUSTAINED non-finiteness (loss goes nan and stays nan — real
    divergence) rather than on the same number of records scattered across a whole run. Mixed-precision
    (fp16/AMP) training legitimately logs a handful of `grad_norm: inf` steps while the loss scaler warms
    up and then recovers; a cumulative count would let those isolated overflows accumulate to the
    threshold over a multi-hour run and discard a healthy, metric-producing result. Resetting on the very
    next finite metric keeps such runs alive while still catching a model that can no longer learn."""
    _PAT = re.compile(
        r"(loss|grad_norm|grad[ _]norm)['\"]?\s*[:=]\s*[\[]?\s*"
        r"(nan|[-+]?inf(?:inity)?)\b", re.IGNORECASE)
    # A metric record whose value IS a finite number (any of the same loss/grad_norm keys). Its presence
    # means training recovered on this step, so it clears the consecutive non-finite streak. `_PAT` is
    # checked first, so a line that reports BOTH a finite and a non-finite metric counts as non-finite.
    _FINITE = re.compile(
        r"(loss|grad_norm|grad[ _]norm)['\"]?\s*[:=]\s*[\[]?\s*"
        r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\b", re.IGNORECASE)
    _BREAK = re.compile(r"[\r\n]")

    def __init__(self, threshold: int = 5):
        self.threshold = threshold
        self.hits = 0
        self._buf = ""

    def _observe(self, ln: str) -> None:
        # Non-finite wins over finite on the same line; a purely-finite metric record resets the streak;
        # a line with no recognizable metric leaves the streak untouched (most output is neither).
        if self._PAT.search(ln):
            self.hits += 1
        elif self._FINITE.search(ln):
            self.hits = 0

    def feed(self, text: str) -> bool:
        """Accept a streamed chunk; return True once divergence is CONFIRMED (>= threshold CONSECUTIVE
        non-finite loss/grad records). Idempotent-safe to call after firing."""
        self._buf += text
        *lines, self._buf = self._BREAK.split(self._buf)  # keep the trailing partial record for next chunk
        if len(self._buf) > 8192:                    # bound a pathological no-newline stream
            self._buf = self._buf[-8192:]
        for ln in lines:
            self._observe(ln)
        return self.hits >= self.threshold

    def finish(self) -> bool:
        """Observe the final unterminated record at EOF. Safe to call more than once."""
        final, self._buf = self._buf, ""
        if final:
            self._observe(final)
        return self.hits >= self.threshold


def _tee_drain(proc, log_path, timeout, max_output_bytes, cancel, health_check=False,
               stall_timeout=None):
    """Drain `proc`'s stdout+stderr concurrently in fixed-size binary chunks: mirror them to
    `log_path` (a live, tail-able combined log) while accumulating bounded per-stream tails.
    Honors the same wall-clock timeout + cancel-event tree-kill as the buffered path. Reader
    threads (daemon) own the pipes so the parent never deadlocks on a chatty child.  Chunked reads
    matter for the no-newline case: `readline()` can allocate an arbitrarily large candidate-owned
    line before returning, which bypasses any cap applied after the read.

    `stall_timeout` (optional seconds): a STALL watchdog for long stages (training/eval). A hung
    child — a distributed/DataParallel finalize deadlock, a wedged CUDA op, a lock never released —
    stays ALIVE producing NO output, so the plain wall-clock deadline would burn the WHOLE (often
    multi-hour) `timeout` before killing it. When set, if NOTHING is written to either stream for
    `stall_timeout` seconds while the child is still running, tree-kill it early with a STALLED
    marker (distinct from a real timeout: the training already printed everything it was going to)."""
    import codecs
    import threading
    import time as _time

    cap = max(max_output_bytes * 4, 256_000)        # bound memory; the FILE (when set) keeps the full log
    read_chunk = 64 * 1024                          # hard upper bound for one pipe read/allocation
    logf = None
    if log_path:                                    # log_path=None -> memory-bounded drain, no file
        try:
            logf = open(log_path, "a", encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # ValueError too: an embedded NUL in the path raises it (not OSError), which would escape
            # here AFTER the child was spawned and leak the process tree (arch-review §3 P0-7 / §4 P1-5).
            # Degrade to no live-file; the drain + deadline + tree-kill below still run.
            logf = None
    lock = threading.Lock()
    bufs: dict[str, list[bytes]] = {"out": [], "err": []}
    # CODEX AGENT: stdout and stderr need independent record buffers (never splice two partial lines),
    # but one shared threshold. Framework loggers commonly use stderr while user metrics use stdout.
    monitors = ({"out": _StageHealthMonitor(), "err": _StageHealthMonitor()}
                if health_check else None)
    health_lock = threading.Lock()
    diverged = threading.Event()
    stalled = threading.Event()
    # Last-output clock for the STALL watchdog: any chunk on either stream bumps it (monotonic). The
    # main loop kills the child if it goes quiet for `stall_timeout` while still alive. A mutable holder
    # (not a bare float) so the pump threads and the wait loop share ONE value without a reassignment race.
    last_output = [_time.monotonic()]

    def _observe_health(key: str, text: str = "", *, final: bool = False) -> None:
        if monitors is None:
            return
        with health_lock:
            monitor = monitors[key]
            if text:
                monitor.feed(text)
            if final:
                monitor.finish()
            if (not diverged.is_set()
                    and sum(item.hits for item in monitors.values()) >= monitor.threshold):
                diverged.set()

    def _pump(stream, key):
        size = 0
        # A decoder is needed to feed the health monitor even when there is no live log file.
        scan = monitors is not None
        decoder = (codecs.getincrementaldecoder("utf-8")("replace")
                   if (logf is not None or scan) else None)
        try:
            # BufferedReader.read1 returns currently-available pipe data (up to read_chunk), so live
            # logs still update promptly; the fallback keeps the helper friendly to simple test fakes.
            read = getattr(stream, "read1", None) or stream.read
            while True:
                chunk = read(read_chunk)
                if not chunk:
                    break
                if isinstance(chunk, str):          # defensive compatibility with a text-stream fake
                    chunk = chunk.encode("utf-8", "replace")
                buf = bufs[key]
                buf.append(chunk)
                size += len(chunk)
                last_output[0] = _time.monotonic()   # STALL watchdog: fresh output resets the quiet clock
                if size > cap * 2:                  # collapse to the last `cap` bytes (metric is last line)
                    joined = b"".join(buf)[-cap:]
                    buf.clear()
                    buf.append(joined)
                    size = len(joined)
                if decoder is not None:
                    text = decoder.decode(chunk, final=False)
                    if logf is not None:
                        with lock:
                            logf.write(text)
                            logf.flush()
                    if scan and text:
                        _observe_health(key, text)
        except Exception:
            pass
        finally:
            if decoder is not None:
                try:
                    text = decoder.decode(b"", final=True)
                    if text and logf is not None:
                        with lock:
                            logf.write(text)
                            logf.flush()
                    if scan:
                        _observe_health(key, text, final=True)
                except Exception:
                    pass
            try:
                stream.close()
            except Exception:
                pass

    t_out = threading.Thread(target=_pump, args=(proc.stdout, "out"), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc.stderr, "err"), daemon=True)
    t_out.start()
    t_err.start()
    timed_out = False
    deadline = _time.monotonic() + timeout
    while True:
        try:
            proc.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if diverged.is_set():
                # Health monitor confirmed a non-finite loss -> tree-kill NOW instead of burning the whole
                # (often multi-hour) timeout on a model that can no longer learn. Not a timeout: the killed
                # child exits non-zero -> command_eval's stage-failure path fires with the DIVERGED marker.
                _kill_tree(proc)
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                break
            if (stall_timeout and not stalled.is_set()
                    and (_time.monotonic() - last_output[0]) >= stall_timeout):
                # STALL watchdog: the child is alive but has emitted NOTHING for `stall_timeout` — a hung
                # distributed finalize / wedged CUDA op / deadlock that would otherwise sit until the full
                # (multi-hour) deadline. Tree-kill NOW. NOT a timeout: mark it STALLED so the failure reason
                # is honest and any metric it already printed before going quiet stays salvageable.
                stalled.set()
                _kill_tree(proc)
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                break
            if (cancel is not None and cancel.is_set()) or _time.monotonic() >= deadline:
                _kill_tree(proc)
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                timed_out = True
                break
    # Let the final lines flush before we read the buffers.
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    marker = ("\n‼ LOOPLAB health-check: training DIVERGED — non-finite loss/grad_norm "
              "reported repeatedly; aborting the stage early.\n")
    stall_marker = (f"\n‼ LOOPLAB health-check: stage STALLED — no output for "
                    f"{int(stall_timeout or 0)}s while the process stayed alive (likely a hung "
                    "distributed finalize / wedged CUDA op / deadlock); aborting the stage early.\n")
    active_marker = stall_marker if stalled.is_set() else marker
    if (diverged.is_set() or stalled.is_set()) and logf is not None:
        try:
            logf.write(active_marker)
            logf.flush()
        except Exception:
            pass
    if logf is not None:
        try:
            logf.close()
        except Exception:
            pass
    rc = proc.returncode if proc.returncode is not None else -1
    out = b"".join(bufs["out"]).decode("utf-8", "replace")
    err = b"".join(bufs["err"]).decode("utf-8", "replace")
    if diverged.is_set() or stalled.is_set():
        err += active_marker
        # A short process can exit before the 250 ms parent poll observes the flag. A set flag means we
        # deliberately tree-killed the stage, so a lingering zero status must NOT read as healthy; fail
        # closed after the drain. (STALLED keeps timed_out False so command_eval can still salvage a metric
        # the child printed before it went quiet — the STALLED marker tells the agent what happened.)
        if rc == 0:
            rc = -1
    return rc, out, err, timed_out


def _kill_tree(proc: "subprocess.Popen") -> None:
    try:
        import psutil  # optional (extras: proc)

        # CODEX AGENT: production installs psutil, but this branch snapshots descendants while the
        # parent can still fork. A late DataLoader worker can escape after children() returns, then keep
        # using a GPU the scheduler releases. Kill through the process-group/Job boundary (or suspend
        # and reap to a stable tree), and cover the proc-extra path with a fork-during-kill regression.
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
                 mem_bytes: Optional[int] = None, fsize_bytes: Optional[int] = None,
                 **_: object):  # ignore tier-specific kwargs (symmetry with DockerSandbox)
        self.python = python or sys.executable
        self.max_output_bytes = max_output_bytes
        # Best-effort resource caps on the eval child (None = off). See run_argv's preexec_fn:
        # mem_bytes = RLIMIT_AS (virtual, off for CUDA/torch); fsize_bytes = RLIMIT_FSIZE (per-file,
        # off where large checkpoints are written).
        self.mem_bytes = mem_bytes
        self.fsize_bytes = fsize_bytes

    def run(self, code: str, workdir: str, timeout: float = 30.0,
            env: Optional[dict] = None, cancel=None) -> RunResult:
        wd = Path(workdir).resolve()  # absolute -> safe regardless of caller's cwd
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "solution.py").write_text(code, encoding="utf-8")
        rc, out, err, to = _run_argv(
            [self.python, "solution.py"],  # by name, relative to cwd -> no path doubling
            str(wd), timeout, env, self.max_output_bytes, cancel,
            mem_bytes=self.mem_bytes, fsize_bytes=self.fsize_bytes)
        # Discard metric/trials/extras from a TIMED-OUT run: a process killed at the deadline may have
        # printed a partial/misleading metric line before hanging. Matches DockerSandbox.run and
        # command_eval.run_command_eval, which both null these out on timeout.
        return RunResult(exit_code=rc, stdout=out, stderr=err,
                         metric=(None if to else _parse_metric(out)), timed_out=to,
                         extra_metrics=(None if to else (json_line_extras(out) or None)),
                         trials=(None if to else json_line_trials(out)))


class DockerSandbox:
    """untrusted tier (ADR-13). Real boundary: the solution runs inside ``docker run
    --network none`` with the scratch workdir bind-mounted at /work, so arbitrary code can't
    reach the network or the host FS outside the mount. Fails LOUDLY if the docker CLI is
    absent rather than silently degrading the boundary."""

    def __init__(self, image: str = "python:3.12-slim", network: str = "none",
                 max_output_bytes: int = 64_000, runtime: Optional[str] = None,
                 mem: str = "4g", cpus: str = "", **_: object):
        self.image = image
        self.network = network
        self.max_output_bytes = max_output_bytes
        # Resource caps for the untrusted tier: the whole point of this tier is to protect other
        # tenants, but before this the solution.py path had NO memory/cpu bound and ran with default
        # caps as root — a candidate could OOM the host or saturate every core. `mem`/`cpus` are
        # generous, configurable defaults; "" disables a given cap. (gVisor stops kernel escape but
        # NOT resource exhaustion, so these matter even on the hostile runtime.)
        self.mem = mem
        self.cpus = cpus
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
        # Bound BEFORE int() and BEFORE embedding the deadline into the container argv.  Bounding only
        # inside host-side run_argv is too late: NaN/inf crash int(), while a huge finite value leaves
        # the daemon-owned container running long after the bounded docker CLI has been killed.
        timeout = finite_timeout(timeout, 30.0)
        gpu_args = docker_gpu_argv(env, runtime=self.runtime)
        envs = []
        for k, v in docker_gpu_env(env, gpu_args=gpu_args).items():
            # Pass the reconciled environment into the container explicitly.  GPU pins are enforced
            # by ``--gpus``; CPU pins also override CUDA-image NVIDIA defaults at injection time.
            envs += ["-e", f"{k}={v}"]
        # In-container self-limit (coreutils `timeout`): killing the host `docker run` CLI on
        # timeout does NOT stop the daemon-owned container, so bound it from INSIDE — the
        # container exits at `timeout` and `--rm` removes it, so a runaway leaks at most ~timeout
        # seconds even if the host kills the client first. Host timeout gets a grace margin.
        secs = max(1, int(timeout))
        rt = ["--runtime", self.runtime] if self.runtime else []   # B4+ gVisor/Kata isolation tier
        # Resource + privilege hardening for the untrusted tier: bound memory/cpu, drop all Linux
        # capabilities, and forbid privilege escalation. (--user is deliberately NOT set — the bind-
        # mounted workdir is host-owned, so a non-root uid often can't write predictions/artifacts.)
        caps = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        if self.mem:
            caps += ["--memory", str(self.mem)]
        if self.cpus:
            caps += ["--cpus", str(self.cpus)]
        argv = (["docker", "run", "--rm", "--network", self.network, *rt, *gpu_args,
                 "--pids-limit", "1024",      # fork-bomb guard (review C1: no pids limit before)
                 *caps,
                 "-v", f"{wd.as_posix()}:/work", "-w", "/work"] + envs
                + [self.image, "timeout", "-k", "5", str(secs), "python", "solution.py"])
        rc, out, err, to = _run_argv(argv, str(wd), timeout + 15.0, None, self.max_output_bytes, cancel)
        # See docker_timed_out: both 124 (SIGTERM at deadline) and 137 (SIGKILL escalation past the
        # `-k 5` grace) are this run's wall-clock timeout, not an OOM.
        timed_out = to or docker_timed_out(rc)
        return RunResult(exit_code=rc, stdout=out, stderr=err,
                         metric=(None if timed_out else _parse_metric(out)), timed_out=timed_out,
                         extra_metrics=(None if timed_out else (json_line_extras(out) or None)),
                         trials=(None if timed_out else json_line_trials(out)))


def make_sandbox(trust_mode: str = "trusted_local", *, image: Optional[str] = None,
                 mem_local: str = "", fsize_local: str = "", **kwargs) -> Sandbox:
    """Select the sandbox tier from the trust mode (ADR-13). `image` is routed only to the
    Docker tier (the subprocess tier ignores it); `mem_local`/`fsize_local` (human sizes like "8g")
    are the trusted-local RLIMIT_AS host-OOM and RLIMIT_FSIZE disk-fill caps, routed only to the
    subprocess tier."""
    if trust_mode == "trusted_local":
        return SubprocessSandbox(mem_bytes=parse_mem_bytes(mem_local),
                                 fsize_bytes=parse_mem_bytes(fsize_local), **kwargs)
    if trust_mode == "untrusted":
        return DockerSandbox(image=image or "python:3.12-slim", **kwargs)
    if trust_mode == "hostile":
        # B4+ true-isolation tier: shared-kernel container hardening is NOT an isolation boundary for
        # untrusted LLM code; run under gVisor (runsc) by default. Override via `runtime`.
        kwargs.setdefault("runtime", "runsc")
        return DockerSandbox(image=image or "python:3.12-slim", **kwargs)
    raise ValueError(f"unknown trust_mode: {trust_mode!r}")
