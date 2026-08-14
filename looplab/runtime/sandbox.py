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

from looplab.core.errors import ConfigRefusal
from looplab.runtime.read_fence import FENCE_DIR_ENV, prepend_pythonpath
from looplab.runtime import landlock as _landlock

# THE secret screen — MOVED to `core/envsafe.py`, re-exported here and NOT copied. Every existing
# `from looplab.runtime.sandbox import is_secret_env` (eight modules, `tests/test_secret_env_pattern.py`)
# resolves to the SAME object, so this stays the one spelling of "withhold this from a child process".
# It moved because the DECLARED ENVIRONMENT rule needs the same screen and its third declarer is
# `core/config.py::Settings.eval_env`, which may not import `runtime` (the layering rule that is also
# why `config._SECRET_ENV_NAME` — the stricter api_key_env sibling — is a duplicate rather than an
# import). One home beats a third copy; `tests/test_secret_env_pattern.py` is still the joint.
from looplab.core.envsafe import SECRET_ENV, is_secret_env  # noqa: F401 (re-export)


def git_subprocess_env() -> dict[str, str]:
    """Credential-free host environment for trusted local Git plumbing.

    Git is not a passive executable: repository/global config may invoke hooks, fsmonitor, textconv,
    credential helpers or external diff programs. Drop every ambient secret and every raw ``GIT_*``
    override, then restore only the vetted config/identity subset required by local Git operations.
    """
    from looplab.core.gitenv import git_config_env

    clean = {
        key: value for key, value in os.environ.items()
        if not is_secret_env(key, value) and not str(key).upper().startswith("GIT_")
    }
    clean.update({str(key): str(value) for key, value in git_config_env().items()})
    return clean

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
    clean = {k: v for k, v in env.items() if not is_secret_env(k, v)} if isinstance(env, dict) else {}
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
    to `[0, MAX_TIMEOUT_S]`: a negative deadline is already fail-SAFE (it fires immediately), and 0
    remains a deliberately-honored sentinel for lower-level callers, so both stay non-fatal rather
    than being rewritten. Every subprocess deadline flows through `run_argv`, which applies this; the
    eval/stage builders apply it too so the bounded value is what gets traced. Note the stricter task
    authoring gates in `EvalSpec`/`validate_stages` REJECT non-finite/non-positive profile/stage
    timeouts outright — this is the defensive back-stop for every other caller."""
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


def require_docker_cli(what: str) -> None:
    """Fail LOUDLY when the docker CLI is missing, rather than silently dropping the boundary.

    Both untrusted tiers open with this check and with the same message modulo one noun (doc 25
    RA-03). The message is part of the contract, not decoration: `trust_mode='untrusted'` is a
    promise that arbitrary code runs behind a container, and the ONLY safe response to "the thing
    that makes that true is not installed" is to refuse and say which knob turns it off.
    """
    import shutil
    if not shutil.which("docker"):
        raise RuntimeError(
            f"trust_mode='untrusted' needs the docker CLI to sandbox the {what}, but it was not "
            "found on PATH. Install Docker or use trust_mode='trusted_local'.")


# The writable scratch a `--read-only` container still needs, and the ONE place it is enumerated.
# It is deliberately the same set `runtime/read_allowlist.py` grants `readwrite` OUTSIDE the node's
# own workdir minus the kernel/device tiers a container gets from the runtime anyway: `/tmp` and
# `/var/tmp` for tempfiles (a `tempfile.mkstemp` on a read-only root raises `OSError 30` from inside
# numpy/torch/pip, i.e. arbitrarily deep in a candidate's imports), and NOT `/dev/shm`, which docker
# mounts as its own tmpfs on every container regardless of `--read-only` (the DataLoader-worker
# channel is therefore already writable and must not be re-mounted, since a second mount would
# silently replace the daemon's `--shm-size`).
READONLY_SCRATCH_DIRS = ("/tmp", "/var/tmp")

# `exec` is spelled EXPLICITLY, and that is not decoration: docker's own default for `--tmpfs` is
# `rw,noexec,nosuid,nodev,size=65536k`, so accepting the default would give a 64 MiB `/tmp` that
# cannot execute — which breaks `pip install` from an sdist (build isolation runs an interpreter out
# of TMPDIR), torch's inductor/`cpp_extension` JIT builds, and any `eval.setup` that unpacks and runs
# something. `noexec` would also buy nothing HERE: the candidate's own code lives on the writable
# `/work` bind and is executed from there by definition, so the exec bit on a scratch dir is not the
# boundary. `nosuid,nodev` are free and stay.
_READONLY_SCRATCH_OPTS = "rw,exec,nosuid,nodev,size={size}"


def readonly_rootfs_argv(size: str) -> list[str]:
    """`--read-only` + the tmpfs scratch that makes it survivable — or `[]` when `size` is empty.

    THE ROOT FILESYSTEM IS THE ONE THING THIS TIER NEVER BOUNDED. `--cap-drop ALL`,
    `--security-opt no-new-privileges`, `--pids-limit`, `--network none`, `--memory`/`--cpus` were
    all here; the image's own filesystem was writable, so candidate code ran as root over
    site-packages, `/usr/bin`, `/etc` and the interpreter itself. `--rm` bounds how long that lasts
    (one container per `docker run`, and a staged pipeline gets a fresh one per stage — which is
    exactly why `PYTHONUSERBASE` has to point INTO the /work bind for a `setup` to stick), so the
    blast radius is one eval rather than the next node; that is a mitigation, not the boundary, and
    it is worth nothing against an escape chain whose first step is writing the rootfs.

    WHAT MUST STAY WRITABLE, and why `-v {root}:/work` can never become `:ro`:
      * `/work` IS the node's workdir on the host. The eval writes its checkpoints, its stage logs
        and the metric FILE the engine then reads back on host paths through this bind; a `:ro` here
        does not harden anything, it deletes the tier's output channel. The per-source `edit:false`
        enforcement that DOES exist is `make_docker_wrap(binds=…)`'s `--mount …,readonly`, one
        mount per declared data/reference source, which is the right granularity: those are the
        operator's originals, `/work` is the node's own scratch.
      * `/work/.looplab-deps` — `PYTHONUSERBASE` + `PIP_USER`, i.e. every dependency an `eval.setup`
        installs. Inside the bind, so it is writable for free and `--read-only` costs it nothing.
      * `/tmp`, `/var/tmp` — see `READONLY_SCRATCH_DIRS`.

    WHAT THIS BREAKS, which is why it is `Settings.sandbox_readonly_rootfs` and is OFF by default:
      * `apt-get install` in an `eval.setup` stops working. Today it "works" and then evaporates
        with the container (the known limit `make_docker_wrap` already records); under `--read-only`
        it fails loudly instead, which is better but is still a behaviour change.
      * Anything writing under `$HOME` — the HF hub's `.lock` files beside a cache HIT, `datasets`'
        arrow cache, `~/.matplotlib`, a pip cache. `--network none` (this tier's default) already
        forbids the DOWNLOAD half, and matplotlib degrades to a temp dir once `/tmp` is writable,
        but a task with egress enabled and an unbaked model cache will fail here.
      * The NVIDIA container runtime writes into the container rootfs during `--gpus` setup
        (library symlinks + `ldconfig` rewriting `/etc/ld.so.cache`). Whether that lands before or
        after the read-only remount is runtime-dependent, and NOTHING on this box could drive it —
        there is no docker daemon here. Treat GPU + `--read-only` as unproven.

    An UNPARSEABLE size REFUSES rather than silently disabling the flag. `parse_mem_bytes` returns
    None for garbage because a bad `--memory` should not crash an eval; the opposite is right for a
    boundary, and "the operator asked for a read-only rootfs and got a writable one because they
    typed `1gb`" is precisely the silent downgrade `require_docker_cli` exists to prevent.
    """
    spec = str(size or "").strip()
    if not spec:
        return []
    if parse_mem_bytes(spec) is None:
        raise ConfigRefusal(
            f"sandbox_readonly_rootfs={size!r} is not a size docker can mount. Use a byte count or a "
            "k/m/g/t suffix (e.g. '1g'), or '' to leave the container filesystem writable — this "
            "value is a security boundary, so an unreadable one is refused rather than ignored.")
    opts = _READONLY_SCRATCH_OPTS.format(size=spec)
    argv = ["--read-only"]
    for d in READONLY_SCRATCH_DIRS:
        argv += ["--tmpfs", f"{d}:{opts}"]
    return argv


def docker_run_argv(image: str, *, network: str, mount_root: str, workdir: str,
                    runtime: Optional[str] = None, gpu_args: Optional[list] = None,
                    mem: Optional[str] = None, cpus: Optional[str] = None,
                    env_args: Optional[list] = None,
                    extra_mounts: Optional[list] = None,
                    readonly_rootfs: str = "") -> list[str]:
    """The hardened `docker run` prefix BOTH untrusted tiers share, up to and including the image.

    Everything here is a security boundary, and it used to exist twice — once in
    `DockerSandbox.run` (the solution.py tier) and once in `command_eval.make_docker_wrap` (the
    RepoTask command tier) — kept in step by a comment saying "mirror sandbox.DockerSandbox.run".
    That comment is the evidence the arrangement does not work: the caps had ALREADY drifted once,
    and for a while the solution.py path ran with default capabilities as root and no memory or CPU
    bound (recorded in `DockerSandbox.__init__`). Security hardening gets exactly one home.

    What each flag is doing, once:

    * `--rm` — the container is per-run; leaving it behind leaks a writable layer per node.
    * `--network` — the isolation the tier exists for; the caller passes the configured value so a
      task that genuinely needs egress opts in explicitly rather than by default.
    * `--pids-limit 1024` — fork-bomb guard (review C1: there was no pids limit before).
    * `--cap-drop ALL --security-opt no-new-privileges` — no ambient Linux capabilities and no
      setuid escalation from inside. `--user` is deliberately NOT set: the /work bind is host-owned,
      so a non-root uid often cannot write the predictions/artifacts the eval must produce.
    * `--memory` / `--cpus` — the point of this tier is to protect OTHER tenants, and gVisor/Kata
      stop kernel escape but NOT resource exhaustion, so these matter even on the hostile runtime.
      An empty/None value disables that cap, which is how both callers spell "unbounded".
    * `--runtime` — B4+ gVisor ("runsc") / Kata true-isolation tier; absent = the default runtime.
    * `--read-only` + the `--tmpfs` scratch — the container's own FILESYSTEM, from
      `readonly_rootfs_argv`; empty (the default) leaves the image writable exactly as before. Read
      that function for what has to stay writable and what turning this on costs.

    The caller supplies `gpu_args` (from `docker_gpu_argv`) and `env_args` (`-e` pairs built from
    `docker_gpu_env`, which is the single choke point that strips secret-named host vars) because
    the command tier derives extra entries from the same reconciled dict. `workdir` is the
    in-container `-w`, which differs per tier: the solution tier always runs at /work, the command
    tier at the node's subdirectory under it. Ordering among pre-image flags is not significant to
    `docker run`, so the two former spellings — which interleaved `-e` and `-v` differently — remain
    equivalent under this one.
    """
    root = Path(mount_root).resolve()
    caps = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    if mem:
        caps += ["--memory", str(mem)]
    if cpus:
        caps += ["--cpus", str(cpus)]
    return ["docker", "run", "--rm", "--network", network,
            *(["--runtime", runtime] if runtime else []),
            *(gpu_args or []),
            "--pids-limit", "1024",
            *caps,
            *readonly_rootfs_argv(readonly_rootfs),
            *(env_args or []),
            "-v", f"{root.as_posix()}:/work", *(extra_mounts or []),
            "-w", workdir,
            image]


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
    # WHICH CHANNEL each `extra_metrics` key came through: `{name: "declared"|"auto"}`
    # (`core/models.py::EXTRA_METRIC_CHANNELS`). `declared` means an operator-owned `EvalSpec.metrics`
    # reader produced it; `auto` means it was scraped off the candidate's own stdout JSON line with
    # no declaration and no gate. Every producer of `extra_metrics` MUST set this beside it —
    # an untagged key reads back `unknown` at every consumer, which is deliberately not `declared`.
    extra_metrics_provenance: Optional[dict] = None
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
    # DIVERGE watchdog: True when the stage was tree-killed because its live log reported non-finite
    # loss/grad_norm repeatedly. It is the AUTHENTICATED verdict (`run_argv`'s out-of-band `signals`),
    # not the stderr sentinel — the marker is mixed into the candidate's own stderr and is therefore
    # forgeable, which is the same reason `_salvageable_stall` reads the flag rather than the text.
    #
    # It exists because of what happens WITHOUT it: the watchdog kills with SIGKILL and no traceback,
    # which is byte-for-byte the signature `triage.py::_failure_reason` uses to recognise an OOM. On
    # v6 node 5 that misread sent three repair rounds down a memory-reduction path (batch 8192 ->
    # 2048 -> 512 -> 256, ~15 minutes of GPU each) for a training that was diverging, not swapping —
    # while the correct directive (stabilise the loss / drop the LR) was in the same stderr the whole
    # time. A watchdog kill is the ENGINE's own verdict about the run; it must never be inferred from
    # the exit code the engine itself caused.
    diverged: bool = False
    # METRIC SUBJECT (`runtime/metric_subject.py`): the identity of the artifact this number is a
    # claim ABOUT — `{subject_bound, subjects:[{path, identity, digest, digest_mode, producer}],
    # unbound_reason?, subject_stage?}` — captured at the SCORE stage's start.
    #
    # It exists because a `float` has no referent: measured across the six repo runs with an event
    # log, 82 of 83 recorded metrics carry NO provenance at all, and 2 of 83 are provably about bytes
    # the node did not produce. The reader functions in `command_eval` build a `Path`, `stat` it and
    # DISCARD it; this is the field that stops discarding it. None on the `metric_subject="off"` rung
    # and on every path that never reached a metric read.
    metric_subject: Optional[dict] = None


# Distinctive sentinels in the killed stage's stderr, so command_eval/the orchestrator (and run_argv's
# own docker cleanup) can tell a parent-initiated STALL/DIVERGE kill apart from any other non-zero exit
# without threading a new value through run_argv's 4-tuple return. Both markers are appended to the
# returned stderr by _tee_drain, so a substring test on `err` recovers which watchdog fired.
STALL_SENTINEL = "LOOPLAB health-check: stage STALLED"
DIVERGED_SENTINEL = "LOOPLAB health-check: training DIVERGED"


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
    scanner — both the solution.py path (_parse_metric) and the command-eval readers use it.

    `key` reaches here straight from the (LLM-authored, `kind`-only-validated) eval spec, so a
    non-string like `["metrics", "val_acc"]` would raise `TypeError: unhashable type` out of `key in o`
    and escape the eval worker with no terminal event for the node. Abstain instead — same contract as
    `_dig`'s guard on the file_json path."""
    if not isinstance(key, str):
        return None
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


def stdout_extra_metric_channels(extras) -> Optional[dict]:
    """The channel map for a set of extras that ALL came from `json_line_extras`, i.e. off the final
    JSON line of the artifact's own stdout. Every one of them is `auto`.

    Every solution-tier `RunResult` is in that state by construction — the `solution.py` sandboxes
    have no operator reader spec to consult at all — so this is the one spelling both of them use.
    `None` for an empty/absent set, matching the `extra_metrics or None` shape beside it.

    IT TOOK A `code` ARGUMENT UNTIL 2026-08-14 AND THAT WAS THE DEFECT. The engine does splice its
    own source into some artifacts — the speculation calibration's CUDA probe — and calling the four
    numbers that source prints `auto` is a false statement about 1,636 of the 1,642 values in the
    preserved corpus (see `core/models.py::EXTRA_METRIC_ENGINE`). But the way that was fixed here was
    to hand this function the bytes about to run and grant `engine` on a byte-exact prefix match, and
    THE BYTES ARE THE CANDIDATE'S: `SubprocessSandbox.run` writes `code` to `solution.py`, and on a
    repo run that string is written by an external coding agent. The prefix is a public constant in
    the shipped tree, so prefix + one `print` forged the tag — and forged it past the operator's
    `auto_extra_metrics=false` switch, which keeps exactly the authenticated channels.

    So this tier answers `auto` for everything, and that is not a degradation, it is the honest
    answer AT THIS LAYER: `runtime` receives an opaque string and runs it. Who WROTE the string is a
    fact about the engine's own role wiring, one layer up, and it is asserted there —
    `engine/eval_dispatch.py` upgrades the probe's keys through
    `core/models.py::apply_engine_extra_metric_channels` after this call, using
    `engine/speculation_gate.py::engine_authored_artifacts` as the assertion. A third-party
    `Sandbox` that never gets that upgrade under-claims, which is the fail-safe direction.

    NOT inside `json_line_extras` either, for the reason that has not changed: that scanner is handed
    text and nothing else, so who wrote the print statement is exactly the question it cannot ask."""
    from looplab.core.models import EXTRA_METRIC_AUTO
    return {str(k): EXTRA_METRIC_AUTO for k in (extras or {})} or None


# NO back-compat alias for the previous name `auto_extra_metric_channels`, deliberately: it had no
# importer outside this module. The `code` PARAMETER was dropped rather than deprecated for the same
# reason — a caller that still passes it gets a TypeError instead of silently keeping a grant this
# layer is not entitled to make. A stale spelling must be an error.


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


# Fresh single-threaded launcher for the POSIX rlimit caps — see the call site in `run_argv` for why
# `preexec_fn` is not an option under threaded evaluators. Passed as `-c` source (not `-m`) so it
# needs nothing importable in the child; limits arrive as argv so there is no serialization step.
# `execvp` keeps the pid, cwd, env, session and inherited pipes, so everything downstream — the pgid
# `_kill_tree` targets, `proc.pid`, the drain threads — sees exactly the process it would have before.
_RLIMIT_LAUNCHER = """import os, sys
try:
    import resource
except ImportError:                       # no rlimits here — run uncapped rather than not at all
    resource = None
_a = sys.argv[1:]
_argv = _a[3:]
if resource is not None:
    for _res, _val in ((resource.RLIMIT_AS, int(_a[0])), (resource.RLIMIT_FSIZE, int(_a[1]))):
        if _val <= 0:
            continue
        try:
            _soft, _hard = resource.getrlimit(_res)
            _nh = _val if (_hard == resource.RLIM_INFINITY or _val < _hard) else _hard
            resource.setrlimit(_res, (_val, _nh))
        except (ValueError, OSError):
            pass                          # unprivileged / already lower -> run uncapped
try:
    os.execvp(_argv[0], _argv)
except OSError as exc:
    sys.stderr.write("failed to launch: %s\\n" % (exc,))
    sys.exit(127)
"""


def run_argv(argv: list[str], workdir: str, timeout: float,
             env: Optional[dict] = None, max_output_bytes: int = 64_000, cancel=None,
             log_path: Optional[str] = None, mem_bytes: Optional[int] = None,
             fsize_bytes: Optional[int] = None, health_check: bool = False,
             stall_timeout: Optional[float] = None, signals: Optional[dict] = None,
             on_deadline=None, deadline_grace_max_s: Optional[float] = None):
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
    exactly what they did before. None simply keeps no live file (the drain still runs).

    `on_deadline` (optional `tail -> seconds`) + `deadline_grace_max_s`: a ONE-SHOT, clamped
    extension asked for at the wall instead of killing unconditionally — see the deadline branch in
    `_tee_drain` and `_granted_grace` for the bound and for the 22.0 discarded GPU-hours behind it.
    `runtime` may not call an LLM, so the judge lives in the engine and arrives as this callback, the
    same seam shape `command_eval`'s `check_fn` already uses. Omitted (the default) => the historical
    unconditional kill, byte-for-byte.

    THE STALL WATCHDOG OUTRANKS THE JUDGE ON THE WAY IN AND IS OUTRANKED BY IT ON THE WAY OUT, and
    both halves are deliberate. A child that has been silent for a whole `stall_timeout` is
    stall-killed BEFORE its deadline, so `on_deadline` is never called and no grace is asked for —
    that is not a hole, it is the watchdog doing its job: the judge answers "is this stage finishing"
    and a stage that has said nothing for thirty minutes has given it nothing to read. It does NOT
    make the feature unreachable under the production default (`stall_cap` 1800 against a multi-hour
    budget): a stage still talking within thirty minutes of its wall reaches the deadline and IS
    judged, which is exactly the shipped-corpus case the feature was measured on
    (`rubertlite-dense-retrieval` node 72's record ends on a full progress bar). Once a grace IS
    granted, the order reverses — see `grace_until` in `_tee_drain`."""
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
    # Hoisted out of the cidfile guard below because the READ FENCE also needs it: a `docker run`
    # argv launches the CLIENT on the host, and the container it starts inherits nothing from this
    # environment (only what `docker_gpu_env` forwards via `-e`).
    _docker_run = (len(argv) >= 2 and Path(str(argv[0])).stem.lower() in {"docker", "docker.exe"}
                   and argv[1] == "run")
    if _docker_run and "--cidfile" not in argv:
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
            # it writes so a runaway can't fill the disk (SIGXFSZ past the cap). POSIX only; Windows has
            # no rlimit (Job Objects would be the analog). Both cap aggressively (AS is virtual, FSIZE is
            # per-file), so each defaults OFF and the caller opts in only where it fits (CUDA/torch
            # reserve huge virtual; large checkpoints need big files).
            #
            # Applied by an exec'd LAUNCHER, never by `preexec_fn`. Every eval runs on an
            # `anyio.to_thread` worker, and `preexec_fn` executes between fork and exec in a process
            # that has just forked from a MULTI-THREADED parent: it can inherit an interpreter,
            # import, or allocator lock held by another thread at the instant of the fork and deadlock
            # there — before `Popen` even returns, so the timeout, stall and cancel machinery below
            # never gets a chance to run and the run wedges with no diagnosis. The launcher instead
            # runs in a FRESH single-threaded interpreter where no such lock can be inherited, applies
            # the same caps, and `execvp`s the real argv (same pid, so `_kill_tree`/pgid are
            # unaffected). It is passed as `-c` source rather than `-m` so it needs nothing importable.
            # DO NOT combine this with a `docker run` argv. The rewrite would apply RLIMIT_AS/FSIZE
            # to the docker CLI CLIENT (the container is unbounded), and — worse — it replaces
            # argv[0] with sys.executable, so the post-kill `_remove_docker_container(str(argv[0]),
            # ...)` below becomes `<python> rm -f <cid>`: a silent no-op that leaks the daemon-owned
            # container. No caller mixes them today (the Docker tier bounds the container itself, via
            # --memory/--ulimit, which is where those limits belong), so this stays a documented
            # precondition rather than a guard on the universal choke point. A future caller that
            # needs both must capture the docker binary BEFORE this rewrite.
            argv = [sys.executable, "-c", _RLIMIT_LAUNCHER,
                    str(_mem or 0), str(_fsize or 0), "--", *argv]
    # Don't hand the child code the host's secrets (review C2): a `print(os.environ)` or a stack
    # trace would otherwise exfiltrate LLM_API_KEY / cloud creds into the durable stdout tail. Drop
    # env vars whose NAME looks secret, but keep everything a process needs (PATH, SYSTEMROOT, …)
    # and always keep what the engine explicitly passes in `env` (e.g. LOOPLAB_EVAL_SEED).
    base = {k: v for k, v in os.environ.items() if not is_secret_env(k, v)}
    full_env = {**base, **{k: str(v) for k, v in (env or {}).items()}}
    # SOURCE-TREE READ FENCE (runtime/read_fence.py). The engine hands a fenced launch the fence
    # directory in `LOOPLAB_READ_FENCE_DIR`; prepending it to PYTHONPATH is what makes CPython import
    # the generated `sitecustomize` at startup and install the audit hook that refuses reads of the
    # operator's editable SOURCE tree. Done HERE because this is the universal launch choke point —
    # SubprocessSandbox, every command-eval stage, run_setup and the metric adapter all pass through
    # it — and because PYTHONPATH is inherited, so every python the eval spawns is fenced too.
    #
    # An explicit marker, not a filesystem search: discovering the fence by walking up from the cwd
    # would cost a `stat` of an ABSENT file per level per launch, which is 105-950 ms each on the
    # geesefs/S3 mount a run root usually lives on.
    #
    # Skipped for a `docker run` argv: that PYTHONPATH would name a HOST directory the container
    # cannot see, and the untrusted tiers are fenced by construction anyway — the source tree is
    # never bind-mounted into the container (`engine/eval_dispatch.py::_data_binds` binds the
    # workdir plus the declared data/reference sources, and nothing else). The marker itself is left
    # in the env: it is inert without the PYTHONPATH entry and it tells an operator inspecting a
    # container that the run had a fence.
    if not _docker_run:
        prepend_pythonpath(full_env, full_env.get(FENCE_DIR_ENV) or "")
        # KERNEL READ ALLOW-LIST (runtime/landlock.py), the rung the audit hook above cannot reach:
        # `safetensors`, a Rust `File::open`, a child `cat` and a `torchrun` rank raise no `open`
        # audit event at all. The engine hands a launch its derived allow-list in `LOOPLAB_LANDLOCK_ALLOWLIST`
        # (engine/resources.py); absent — which is the default, `Settings.landlock="off"` — nothing
        # here changes and the launch is byte-identical.
        #
        # Wrapped as a LAUNCHER, not a `preexec_fn`, for the reason `_RLIMIT_LAUNCHER` records: evals
        # run from `anyio.to_thread` workers and a `preexec_fn` under a threaded parent is exactly
        # the deadlock shape that pattern exists to avoid. Applied OUTERMOST, after any rlimit wrap,
        # so the ruleset is in force for everything downstream; Landlock is inherited across `exec`
        # by the kernel, so one application covers the whole process tree.
        #
        # The launch's own workdir is added here rather than by the engine: `wd` is the one path that
        # is per-LAUNCH (the node workdir, the repo root for `setup`, a stage's cwd) and a process
        # that cannot write its own cwd cannot run. Skipped for a `docker run` argv on the same
        # reasoning as the fence — the container never sees these host paths, and the untrusted tiers
        # are bounded by the bind set instead.
        _ll = full_env.get(_landlock.LANDLOCK_ENV) or ""
        if _ll:
            argv = _landlock.launch_argv(
                sys.executable,
                _landlock.format_env([(str(wd), "readwrite")] + _landlock.parse_env(_ll)),
                argv)
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
                                         health_check=health_check, stall_timeout=stall_timeout,
                                         signals=signals, on_deadline=on_deadline,
                                         deadline_grace_max_s=deadline_grace_max_s)
    if docker_cidfile is not None:
        # Defense-in-depth: the cidfile now lives in the host temp dir (unreachable by the container),
        # but never let a cleanup hiccup (a FUSE OSError, or — pre-#5 — untrusted code having replaced
        # the path with a directory so unlink raises IsADirectoryError) turn a normal timeout into an
        # engine-visible crash on the untrusted eval path.
        try:
            # Force-remove the daemon-owned container for EVERY parent-initiated kill, not just a hard
            # timeout. The divergence and stall watchdogs also tree-kill the local `docker run` client but
            # deliberately return timed_out=False; gating removal on `timed_out` alone left their container
            # running while the cidfile (the only cleanup handle) was unlinked below — so the GPU it holds
            # is released by the scheduler and re-pinned to a sibling, double-assigning the device. The
            # STALL/DIVERGE watchdogs append their sentinel to `err` (the intended out-of-band signal, see
            # STALL_SENTINEL), so a substring test recovers a watchdog kill without a 4-tuple change.
            if timed_out or STALL_SENTINEL in err or DIVERGED_SENTINEL in err:
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
        # Set by the last feed()/finish() that saw a FINITE metric record. `_StageHealthPair` runs two
        # monitors against ONE shared threshold and needs to know training recovered on this step in
        # order to clear the SIBLING stream's streak too.
        self.recovered = False
        self._buf = ""

    def _observe(self, ln: str) -> None:
        # Non-finite wins over finite on the same line; a purely-finite metric record resets the streak;
        # a line with no recognizable metric leaves the streak untouched (most output is neither).
        if self._PAT.search(ln):
            self.hits += 1
        elif self._FINITE.search(ln):
            self.hits = 0
            self.recovered = True

    def feed(self, text: str) -> bool:
        """Accept a streamed chunk; return True once divergence is CONFIRMED (>= threshold CONSECUTIVE
        non-finite loss/grad records). Idempotent-safe to call after firing."""
        self.recovered = False
        self._buf += text
        *lines, self._buf = self._BREAK.split(self._buf)  # keep the trailing partial record for next chunk
        if len(self._buf) > 8192:                    # bound a pathological no-newline stream
            self._buf = self._buf[-8192:]
        for ln in lines:
            self._observe(ln)
        return self.hits >= self.threshold

    def finish(self) -> bool:
        """Observe the final unterminated record at EOF. Safe to call more than once."""
        self.recovered = False
        final, self._buf = self._buf, ""
        if final:
            self._observe(final)
        return self.hits >= self.threshold


class _StageHealthPair:
    """The stdout + stderr monitors behind ONE shared divergence threshold.

    The two streams need independent record buffers (a partial line from one must never be spliced
    onto the other), but they are usually ONE logical training log split over two fds — framework
    loggers commonly write to stderr while the user's metrics go to stdout — so threshold evidence
    legitimately straddles both. That makes the streak a property of the PAIR, not of a stream: hits
    SUM across them, and a finite metric record on EITHER stream clears BOTH, because a finite metric
    anywhere means training recovered on that step.

    Clearing only its own stream stranded hits on a stream that then went quiet (a one-off stderr
    dump of a few `loss: nan` tokens with no later finite record THERE): they stayed counted forever
    and let non-sustained non-finiteness on the other stream trip the shared threshold, tree-killing
    a healthy run and breaking `_StageHealthMonitor`'s "CONSECUTIVE, not cumulative" guarantee. Real
    divergence logs no finite metric on either stream, so it is caught exactly as before.

    Thread-safe: both pump threads call `observe` concurrently."""

    def __init__(self, threshold: int = 5):
        import threading           # module-local, like `_tee_drain`'s own import

        self.threshold = threshold
        self.monitors = {"out": _StageHealthMonitor(threshold),
                         "err": _StageHealthMonitor(threshold)}
        self._lock = threading.Lock()

    def observe(self, key: str, text: str = "", *, final: bool = False) -> bool:
        """Feed one stream's chunk (or flush it at EOF); True once divergence is CONFIRMED."""
        with self._lock:
            monitor = self.monitors[key]
            if text:
                monitor.feed(text)
            if final:
                monitor.finish()
            if monitor.recovered:
                for item in self.monitors.values():
                    item.hits = 0
            return sum(item.hits for item in self.monitors.values()) >= self.threshold


def _granted_grace(on_deadline, tail: str, cap) -> float:
    """Seconds of ONE-SHOT extra wall clock a deadline judge may buy. Clamped, and TOTAL — every
    way of not answering is 0.0, so the historical hard kill is what a missing/broken judge gets.

    THE RUNTIME CLAMPS, not the judge. `runtime` may not call an LLM (it imports nothing above
    `core`), so the thing on the other end of this callback is the engine's, and this is the one
    place its answer is bounded. A judge that asks for an hour, for infinity, or for a string gets
    `cap`, `cap` and 0.0 respectively — the ACTION space widens by a bounded amount and the
    candidate's own log, which is what the judge reads, can buy time and nothing else. It cannot buy
    a metric: `read_metric` and the operator's `score` stage are untouched by this, and a graced run
    that still misses the extended deadline reports `timed_out` exactly as before."""
    if on_deadline is None or not cap or float(cap) <= 0:
        return 0.0
    try:
        asked = on_deadline(tail)
    except Exception:  # noqa: BLE001 — a judge that raises must not turn a timeout into a crash
        return 0.0
    try:
        asked = float(asked if asked is not None else 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not (asked > 0):          # covers 0, negatives and NaN (every comparison with NaN is False)
        return 0.0
    return min(asked, float(cap))


def _tee_drain(proc, log_path, timeout, max_output_bytes, cancel, health_check=False,
               stall_timeout=None, signals=None, on_deadline=None, deadline_grace_max_s=None):
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
    # stdout and stderr need independent record buffers (never splice two partial lines) but share one
    # divergence threshold — see `_StageHealthPair`, which owns that policy and its own lock.
    health = _StageHealthPair() if health_check else None
    diverged = threading.Event()
    stalled = threading.Event()
    # Last-output clock for the STALL watchdog: any chunk on either stream bumps it (monotonic). The
    # main loop kills the child if it goes quiet for `stall_timeout` while still alive. A mutable holder
    # (not a bare float) so the pump threads and the wait loop share ONE value without a reassignment race.
    last_output = [_time.monotonic()]
    # ONE-SHOT deadline grace (see the deadline branch below). `graced` records that the judge was
    # ASKED — not that it said yes — so a judge that declines cannot be re-asked every 250 ms for the
    # rest of the wait; `granted` is the seconds actually bought, reported out through `signals` so
    # the caller can say so rather than silently reporting a longer stage.
    graced = [False]
    granted = [0.0]
    # THE TWO CLOCKS MEET HERE. `deadline` bounds TOTAL wall time; `stall_timeout` bounds SILENCE.
    # A granted grace moves the first and says nothing about the second, and until 2026-08-14 the
    # stall branch below — evaluated FIRST in the same 250 ms tick — killed the child it had just
    # been bought for: a stage that printed `100%|##########| 664/664` and went quiet to write its
    # checkpoint was graced 600 s at its 4 s wall and died 0.26 s later, with the durable row
    # claiming `deadline_grace_s: 600.0` for an extension the process never received.
    # `grace_until` is the instant a granted grace expires (== the extended `deadline`), and while
    # `now < grace_until` the SILENCE kill is DEFERRED, never cancelled:
    #   * the judge has just READ THE LIVE LOG and said "this is finishing" — that is strictly more
    #     evidence about this stage than "no bytes for N seconds", which is a proxy for the same
    #     question asked by something that cannot read;
    #   * the watchdog's own justification is not spent here. It exists so a deadlock dies in
    #     minutes instead of burning a multi-hour budget — but at the wall there IS no multi-hour
    #     budget left. The only time still at risk is the grace itself, which `deadline_grace_max_s`
    #     bounds and which the OPERATOR chose to spend. So the silence kill's remaining value inside
    #     the graced window is at most seconds the operator already agreed to; the cost of firing is
    #     the whole stage, in exactly the case the grace exists to rescue.
    # DEFERRED and not SUSPENDED, because a silent child must still die and the ROW must say what
    # killed it: at `grace_until` the deadline and the deferral lift on the same tick, the stall
    # branch is checked first, and a child that never spoke again is killed STALLED (keeping the
    # metric-salvage path a stall has always kept) rather than reported as a plain timeout — while
    # one that did speak falls through to the deadline branch. Either way the row is honest and the
    # wall clock shows the seconds `deadline_grace_s` claims.
    grace_until = [0.0]

    def _current_tail(max_chars: int = 4000) -> str:
        """The recent combined output the deadline judge reads. Bounded, and taken under the pump
        lock so it never splices a half-written chunk."""
        with lock:
            out_b = b"".join(bufs["out"])[-max_chars * 4:]
            err_b = b"".join(bufs["err"])[-max_chars * 4:]
        text = (out_b.decode("utf-8", "replace") + "\n" + err_b.decode("utf-8", "replace"))
        return text[-max_chars:]

    def _observe_health(key: str, text: str = "", *, final: bool = False) -> None:
        if health is not None and health.observe(key, text, final=final):
            diverged.set()

    def _pump(stream, key):
        size = 0
        # A decoder is needed to feed the health monitor even when there is no live log file.
        scan = health is not None
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
            _wait_without_reaping(proc, 0.25)
            # The child is finished but still UN-REAPED, so its pid — and therefore the process
            # GROUP id — is still reserved. Sweep now: a clean exit 0 is not proof the group is
            # empty, and a descendant that outlives it keeps the GPU the scheduler is about to hand
            # to the next node (and holds our pipe write ends open, stalling the drain below).
            _reap_process_group(proc)
            proc.wait()
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
                    and _time.monotonic() >= grace_until[0]
                    and (_time.monotonic() - last_output[0]) >= stall_timeout):
                # STALL watchdog: the child is alive but has emitted NOTHING for `stall_timeout` — a hung
                # distributed finalize / wedged CUDA op / deadlock that would otherwise sit until the full
                # (multi-hour) deadline. Tree-kill NOW. NOT a timeout: mark it STALLED so the failure reason
                # is honest and any metric it already printed before going quiet stays salvageable.
                # The `grace_until` conjunct above is the ONE thing that holds this off: inside a granted
                # grace the silence kill is deferred to the end of that window (see `grace_until`).
                stalled.set()
                _kill_tree(proc)
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                break
            if cancel is not None and cancel.is_set():
                # NEVER graced. The operator asked for this to stop.
                _kill_tree(proc)
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                timed_out = True
                break
            if _time.monotonic() >= deadline:
                # THE DEADLINE IS A NUMBER THAT CANNOT SEE A PROGRESS BAR AT 664/664. Measured on
                # the shipped corpus: all four `stage_finished.status == "timeout"` rows land exactly
                # on their 4 h / 6 h / 8 h wall and together discarded 22.0 GPU-hours, and
                # `rubertlite-dense-retrieval` node 72's captured tail — the whole record of a
                # 12.45-hour node — ends `100%|##########| 664/664 [00:17<00:00, 38.13it/s]`. A
                # stage two seconds from writing its checkpoint and a stage that will never finish
                # produce the same fact at the deadline: elapsed >= timeout. Only something reading
                # the log can tell them apart, so at the wall the judge is ASKED once — bounded by
                # `deadline_grace_max_s`, at most ONE grace per command, and never on a cancel.
                # With no judge wired this is byte-for-byte the unconditional kill it always was.
                _grace = 0.0
                if on_deadline is not None and not graced[0]:
                    graced[0] = True
                    _grace = _granted_grace(on_deadline, _current_tail(), deadline_grace_max_s)
                if _grace > 0:
                    deadline = _time.monotonic() + _grace
                    granted[0] += _grace
                    # The grace has to mean something to the SILENCE clock too, or it buys nothing
                    # for the very stage it was granted to: a child quiet at its wall is quiet one
                    # tick later, and the stall branch above runs first. Defer that branch for
                    # exactly the window bought — never past it (`grace_until` == the extended
                    # `deadline`), so a genuinely deadlocked child still dies, and dies saying so.
                    grace_until[0] = deadline
                    if logf is not None:
                        # UNDER `lock`, unlike the two watchdog markers at the bottom of this
                        # function: those are written after `t_out`/`t_err` have joined, so nothing
                        # else can be in `logf`. This one is written while BOTH pump threads are
                        # still live and writing to the same handle, so an unlocked write here
                        # interleaves into the middle of a training log line.
                        try:
                            with lock:
                                logf.write(f"\n‼ LOOPLAB deadline: the stage reached its time budget "
                                           f"and a live-log judge granted ONE extension of "
                                           f"{int(_grace)}s.\n")
                                logf.flush()
                        except (OSError, ValueError):
                            pass
                    continue
                _kill_tree(proc)
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                timed_out = True
                break
    # Let the final lines flush before we read the buffers. Every terminal path above has already
    # swept the group — the kill branches through `_kill_tree`, the ordinary-exit branch through
    # `_reap_process_group` — so nothing the child left behind is still writing into these pipes.
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    marker = (f"\n‼ {DIVERGED_SENTINEL} — non-finite loss/grad_norm "
              "reported repeatedly; aborting the stage early.\n")
    stall_marker = (f"\n‼ LOOPLAB health-check: stage STALLED — no output for "
                    f"{int(stall_timeout or 0)}s while the process stayed alive (likely a hung "
                    "distributed finalize / wedged CUDA op / deadlock); aborting the stage early.\n")
    # BOTH watchdogs can fire: a stage that logs non-finite records and then goes SILENT is stall-killed
    # first, and the drain's final decoder flush (`_observe_health(final=True)`) then confirms the
    # divergence from the trailing unterminated record. DIVERGED takes precedence — it is a fail-closed
    # verdict, while STALLED exists precisely to SALVAGE a metric printed before the hang, so reporting
    # the stall would hand the salvage gate a metric from a training this watchdog meant to reject.
    # (`signals` carries both flags; `command_eval` reads the pair — see `_salvageable_stall`.)
    active_marker = marker if diverged.is_set() else stall_marker
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
    # AUTHENTICATED watchdog verdict. The markers below are appended to `err`, which also carries the
    # CANDIDATE's own stderr, so `SENTINEL in err` is forgeable: a solution could print the STALLED
    # sentinel, print a metric, and exit non-zero — and the stall-salvage gate
    # (`metric is not None and not timed_out and (exit == 0 or stalled)`) would accept the crashed run.
    # Worse on the staged path, which is the only `health_check=True` caller: the DIVERGE watchdog
    # deliberately forces a non-zero exit against the candidate's will, and a forged stall marker
    # converted that fail-closed verdict back into an accepted metric. `signals` is the out-of-band
    # channel the candidate cannot write to; callers that pass it must prefer it over the text test.
    if signals is not None:
        signals["stalled"] = stalled.is_set()
        signals["diverged"] = diverged.is_set()
        # The grace is OUT-OF-BAND for the same reason the two watchdog verdicts are: the caller must
        # be able to say "this stage ran 6h20m because a judge bought 20 minutes" without reading it
        # back out of text the candidate also writes into. Written ONLY when seconds were actually
        # bought — the two watchdog flags above are always-present booleans because their absence and
        # their `False` mean the same thing, and this key's presence is itself the fact.
        if granted[0] > 0:
            signals["deadline_grace_s"] = granted[0]
    if diverged.is_set() or stalled.is_set():
        err += active_marker
        # A short process can exit before the 250 ms parent poll observes the flag. A set flag means we
        # deliberately tree-killed the stage, so a lingering zero status must NOT read as healthy; fail
        # closed after the drain. (STALLED keeps timed_out False so command_eval can still salvage a metric
        # the child printed before it went quiet — the STALLED marker tells the agent what happened.)
        if rc == 0:
            rc = -1
    return rc, out, err, timed_out


def _wait_without_reaping(proc: "subprocess.Popen", timeout: float) -> None:
    """Block up to `timeout` for the child to EXIT, deliberately leaving it un-reaped.

    `Popen.wait` collects the child, which frees its pid — and with it the process GROUP id that
    `_reap_process_group` still needs in order to signal descendants. `waitid(..., WNOWAIT)` reports
    the exit and leaves the zombie in place, so the group id cannot be recycled underneath us and the
    sweep can never land on a stranger's group. Raises `TimeoutExpired` exactly like `Popen.wait`, so
    the caller's loop is unchanged. Falls back to `Popen.wait` where WNOWAIT does not exist
    (Windows), which simply keeps today's behaviour there.
    """
    import time as _time

    if getattr(proc, "returncode", None) is not None:
        return
    pid = getattr(proc, "pid", None)
    # A Popen-shaped test fake (or a handle without a real pid) has no process group to preserve, so
    # there is nothing WNOWAIT would buy — take the ordinary wait.
    if (os.name == "nt" or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT")):
        proc.wait(timeout=timeout)
        return
    deadline = _time.monotonic() + max(0.0, timeout)
    while True:
        try:
            done = os.waitid(os.P_PID, pid,
                             os.WEXITED | os.WNOHANG | os.WNOWAIT)   # type: ignore[attr-defined]
        except (ChildProcessError, OSError, ValueError):
            # Already collected elsewhere, or an unusable handle — fall back to the normal wait so
            # the caller still sees an exit rather than spinning to its deadline.
            proc.wait(timeout=max(0.0, deadline - _time.monotonic()))
            return
        if done is not None:
            return
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(getattr(proc, "args", None), timeout)
        # WNOWAIT has no blocking-with-timeout form, so this polls. 50 ms bounds the extra exit
        # latency well under the 0.25 s cadence the caller's stall/cancel checks already run at,
        # while keeping the idle cost of a multi-hour eval to a handful of syscalls per second.
        _time.sleep(min(0.05, remaining))


def _reap_process_group(proc: "subprocess.Popen") -> None:
    """SIGKILL whatever is still alive in the exited child's process group.

    A metric-producing parent can exit 0 while a descendant it redirected keeps running — a
    DataLoader worker, a background trainer, a `nohup`-ed helper — and that descendant keeps holding
    the GPU the scheduler is about to hand to the next node. Correct ONLY while the leader is still a
    zombie: its pid, and therefore the group id, cannot be recycled until it is collected, which is
    exactly what `_wait_without_reaping` guarantees at the one call site.
    """
    pid = getattr(proc, "pid", None)
    if os.name == "nt" or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return
    if getattr(proc, "returncode", None) is not None:
        # Same fence `_kill_tree` documents: once the child has been COLLECTED its pid is free for
        # the OS to reuse, so `getpgid(pid)` can name a stranger's group. The one caller reaches here
        # while the child is still a zombie; anything else must not signal on a guess.
        return
    try:
        pgid = os.getpgid(pid)
        own = os.getpgid(0)
    except (OSError, ValueError):
        return
    if pgid == own:
        # `start_new_session=True` did not take effect (or this is a Popen the caller built itself),
        # so the child shares the ENGINE's group — signalling it would kill the engine.
        return
    try:
        os.killpg(pgid, 9)
    except (OSError, ValueError):
        pass


def _kill_tree(proc: "subprocess.Popen") -> None:
    # POSIX: kill the whole SESSION/process group in ONE atomic syscall. The child was spawned with
    # start_new_session=True (see run_argv), so it is its own session/group leader — os.getpgid(pid) ==
    # pid, so killpg targets ONLY the eval's group, never the engine — and SIGKILL to the group reaps
    # every descendant, INCLUDING one forked DURING the kill, with no race. The psutil path snapshots
    # parent.children() then kills each, which races a late fork: a DataLoader/worker spawned after the
    # snapshot escapes and keeps using a GPU the scheduler then releases. So on POSIX do the group kill
    # first and reserve psutil for Windows / a fallback if the group kill itself fails.
    # NEVER signal an already-REAPED process: once `wait()`/`communicate()` has collected the child,
    # its PID is free for the OS to reuse, and `os.getpgid(reused_pid)` then names a STRANGER's group.
    # Callers really do reach here post-reap (`agents/cli_agent.py`'s `except BaseException: _kill_tree(p)`
    # runs after `communicate()` returned), and a helper the engine spawned with plain `subprocess.run`
    # shares the ENGINE's process group — so an unguarded killpg could SIGKILL the engine itself. This is
    # the same PID-reuse hazard `runtime/bg_tasks.py` documents and guards with `deadline_lock`; here the
    # `returncode` check is the fence, and it must precede BOTH the group kill and the psutil path.
    # Read `returncode` ONLY — never call `poll()` here. `poll()` REAPS an exited child, and reaping
    # is exactly what frees the PID for reuse, so asking destroys the evidence this fence needs. Worse,
    # it made the function RETURN on a child that had just exited while its GROUP was still alive:
    # `_tee_drain` reaches here right after a 250 ms `proc.wait` timeout, so a leader that exits in
    # that window left its DataLoader/torchrun descendants running on a GPU the scheduler then
    # released (reproduced: the grandchild survived `_kill_tree`). A ZOMBIE retains its pid and pgid,
    # so killpg still reaches the survivors; `returncode` is set only when the CALLER already reaped
    # (`cli_agent.py`'s post-`communicate()` path), which is the one state where the pid may name a
    # stranger's group.
    if getattr(proc, "returncode", None) is not None:
        return
    # `start_new_session=True` did not take effect (or the caller built this Popen itself), so the
    # child shares the ENGINE's group and killpg would SIGKILL the engine. Mirrors the guard in
    # `_reap_process_group` above; the pre-existing killpg here had the `returncode` fence only,
    # which does not cover a LIVE same-group child. This DISQUALIFIES the group syscall, not the
    # kill: fall through to the psutil path, which walks the tree by PID and so never touches the
    # engine. Returning outright instead would leave the whole tree running — trading "we killed
    # ourselves" for "the eval keeps burning a GPU the scheduler already released".
    #
    # RESIDUAL GAP, stated rather than papered over: that rescue reaches the tree only while the
    # LEADER is still a process we can enumerate from. If a same-group leader exited during the
    # 250 ms `proc.wait` window (`_tee_drain` — the same race the `returncode` fence documents for
    # the non-same-group case) its live descendants were reparented to init/a subreaper, so
    # `parent.children(recursive=True)` returns NOTHING and that tree is killed by no path at all:
    # killpg is disqualified, psutil finds an empty tree, the last resort is leader-only. It cannot
    # be closed from here — `_kill_tree` runs after the fact, so there is no earlier moment to
    # snapshot descendants from, and the only alternative, sweeping every process in the group, is
    # sweeping the ENGINE's own group, exactly what the disqualification above exists to prevent.
    # The fix belongs at the SPAWN site: `start_new_session=True` (run_argv) makes the leader its own
    # group leader, and then the atomic killpg below is what runs and the gap never opens.
    same_group = False
    if os.name != "nt":
        try:
            pgid, own = os.getpgid(proc.pid), os.getpgid(0)
        except (OSError, ValueError):
            pgid = own = None
        same_group = pgid is not None and pgid == own
        if pgid is not None and not same_group:
            try:
                os.killpg(pgid, 9)
                return
            except (OSError, ValueError):
                pass
    try:
        import psutil  # optional (extras: proc) — Windows tree kill, or a POSIX fallback if killpg failed

        parent = psutil.Process(proc.pid)
        # Each kill guarded ON ITS OWN. A child that exits between the `children()` snapshot and its
        # own `kill()` raises NoSuchProcess — routine with churning DataLoader workers — and under a
        # single shared try/except that one vanished worker escaped to the enclosing `except: pass`,
        # skipping every REMAINING child AND `parent.kill()`. This is the designated kill path for
        # the same_group case, whose last-resort fallback is a leader-only `proc.kill()`, so the
        # survivors were leaked into the engine's own group where no later group sweep can reach
        # them. AccessDenied is swallowed for the same reason: it means this one process cannot be
        # killed, never that the rest of the tree should be spared.
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            parent.kill()
        except psutil.NoSuchProcess:
            pass
        return
    except Exception:
        pass
    # Last-resort fallback (no psutil, or the group kill above failed): OS-branched WHOLE-TREE kill.
    # Plain proc.kill() on Windows ends only the direct child, orphaning grandchildren
    # (DataLoader/worker/nested-train subprocesses) — so use `taskkill /T`; on POSIX kill the group.
    # `same_group` gates that killpg for the same reason it gated the one above: this branch is
    # reached when psutil is absent, which is exactly when an unguarded group kill would take the
    # engine down with the child. There the single `proc.kill()` below is all that is safe.
    #
    # NEITHER Windows path is ATOMIC, and that is a known, unfixed gap rather than an oversight. Both
    # psutil's `children(recursive=True)` above and `taskkill /T` here ENUMERATE a tree and then kill
    # its members, so a process the doomed tree spawns between the enumeration and the kill survives
    # — the exact race a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` does not have, because
    # membership is inherited at CreateProcess time and closing the handle kills everything at once.
    # It is not adopted here because the fix does not live in this function: the job must be created
    # and the child ASSIGNED to it at SPAWN (`run_argv`), with the breakaway cases handled, which is
    # a Windows-only code path no box in this repo can execute (this one is Linux; the suite has
    # never run under `os.name == "nt"`). Note the scope of what it would buy: this is the
    # trusted_local tier, which this module's own header states is NOT a security boundary — under
    # the Docker tiers a runaway is bounded by `--pids-limit`, the in-container coreutils `timeout`
    # and `--rm`, none of which routes through here. So the missing Job Object is a leaked-process
    # robustness fix on the operator's own box, not a hole in the untrusted tier.
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        elif same_group:
            proc.kill()
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
        _extras = None if to else (json_line_extras(out) or None)
        return RunResult(exit_code=rc, stdout=out, stderr=err,
                         metric=(None if to else _parse_metric(out)), timed_out=to,
                         extra_metrics=_extras,
                         extra_metrics_provenance=stdout_extra_metric_channels(_extras),
                         trials=(None if to else json_line_trials(out)))


class DockerSandbox:
    """untrusted tier (ADR-13). Real boundary: the solution runs inside ``docker run
    --network none`` with the scratch workdir bind-mounted at /work, so arbitrary code can't
    reach the network or the host FS outside the mount. Fails LOUDLY if the docker CLI is
    absent rather than silently degrading the boundary."""

    def __init__(self, image: str = "python:3.12-slim", network: str = "none",
                 max_output_bytes: int = 64_000, runtime: Optional[str] = None,
                 mem: str = "4g", cpus: str = "", readonly_rootfs: str = "", **_: object):
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
        # Container ROOT-FILESYSTEM hardening ("" = off = the historical writable image). Validated
        # HERE rather than at the first `run`: `make_sandbox` is called from `cli/__init__.py::_engine`
        # during startup, so a misspelled size refuses the launch instead of failing the first node's
        # eval — the same "fail loud at START, not mid-sweep" rule the engine's docker-CLI probe uses.
        readonly_rootfs_argv(readonly_rootfs)
        self.readonly_rootfs = readonly_rootfs

    def run(self, code: str, workdir: str, timeout: float = 30.0,
            env: Optional[dict] = None, cancel=None) -> RunResult:
        require_docker_cli("solution")
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
        # Every hardening flag (network, runtime, pids-limit, caps, memory/cpus, mount) comes from
        # the ONE builder both untrusted tiers share — see `docker_run_argv` for what each is for.
        argv = docker_run_argv(
            self.image, network=self.network, mount_root=str(wd), workdir="/work",
            runtime=self.runtime, gpu_args=gpu_args, mem=self.mem, cpus=self.cpus,
            env_args=envs, readonly_rootfs=self.readonly_rootfs,
        ) + ["timeout", "-k", "5", str(secs), "python", "solution.py"]
        rc, out, err, to = _run_argv(argv, str(wd), timeout + 15.0, None, self.max_output_bytes, cancel)
        # See docker_timed_out: both 124 (SIGTERM at deadline) and 137 (SIGKILL escalation past the
        # `-k 5` grace) are this run's wall-clock timeout, not an OOM.
        timed_out = to or docker_timed_out(rc)
        _extras = None if timed_out else (json_line_extras(out) or None)
        return RunResult(exit_code=rc, stdout=out, stderr=err,
                         metric=(None if timed_out else _parse_metric(out)), timed_out=timed_out,
                         extra_metrics=_extras,
                         extra_metrics_provenance=stdout_extra_metric_channels(_extras),
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
    # `Settings.trust_mode` is a free-form string too (this ladder is the vocabulary), so a typo
    # arrives from `-s trust_mode=…` and is an operator input error, not a bug — name the tiers.
    raise ConfigRefusal(f"unknown trust_mode: {trust_mode!r}; choose one of: "
                        "trusted_local, untrusted, hostile")
