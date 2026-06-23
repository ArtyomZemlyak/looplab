"""Command-based evaluation (RepoTask, ADR-7) — generalizes the solution.py-prints-metric
model into "run an operator-declared command in a workdir, then read a metric from a
declared source". The metric reader is pluggable:

  - stdout_json(key)        — last stdout JSON line containing `key` (the current model)
  - stdout_regex(pattern)   — regex over stdout, a capture group cast to float
  - file_json(path, key)    — a metrics file the framework writes (dotted key supported)
  - file_regex(path, ...)   — regex over a file the framework writes

This covers most ML frameworks (TensorBoard/MLflow/W&B all also write local files). A
fully custom tracker is handled by the agent-written `adapter` mode (Phase 3) — not here.

Process management mirrors SubprocessSandbox exactly (reused `_kill_tree`, `RunResult`,
Windows process-group flags, UTF-8 capture) so timeouts/tree-kill behave identically.
"""
from __future__ import annotations

import json
import os
import re
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Optional

from .sandbox import RunResult, _json_line_metric, _run_argv, _to_float


def _dig(obj, key: str):
    """Fetch a possibly-dotted key from nested dicts: 'metrics.val_acc'."""
    cur = obj
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _regex_metric(text: str, pattern: str, group: int) -> Optional[float]:
    # A bad operator-supplied pattern (re.error) or out-of-range group (IndexError) must read
    # as "no metric", not crash the eval.
    try:
        last = None
        for m in re.finditer(pattern, text):   # take the LAST match (final epoch, etc.)
            last = m
        return _to_float(last.group(group)) if last else None
    except (re.error, IndexError):
        return None


def read_metric(stdout: str, workdir: str, spec: dict, wrap=None) -> Optional[float]:
    """Read the metric for one eval according to `spec` (an eval_spec['metric']). Built-in
    readers parse host files/stdout in-process (data, never code). The `adapter` reader EXECS
    agent-authored code, so under the untrusted tier it must run in the same sandbox as the
    eval — pass `wrap` (from make_docker_wrap) to run it inside the container."""
    kind = spec.get("kind", "stdout_json")
    if kind == "stdout_json":
        return _json_line_metric(stdout, spec.get("key", "metric"))
    if kind == "stdout_regex":
        return _regex_metric(stdout, spec["pattern"], int(spec.get("group", 1)))
    if kind in ("file_json", "file_regex"):
        p = Path(workdir) / spec["path"]
        if not p.is_file():
            return None
        # utf-8-sig strips a UTF-8 BOM (common on Windows-written metric files) that would
        # otherwise make json.loads fail / regex miss the first line.
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        if kind == "file_regex":
            return _regex_metric(text, spec["pattern"], int(spec.get("group", 1)))
        try:
            return _to_float(_dig(json.loads(text), spec.get("key", "metric")))
        except json.JSONDecodeError:
            return None
    if kind == "adapter":
        # A (human-ratified, frozen) agent-written module exposing read_metric(workdir)->
        # float, for an arbitrary tracker (TensorBoard/ClearML/custom). Run as a SUBPROCESS
        # in the workdir (not in-process) so it inherits the same timeout/tree-kill harness
        # and can't hang or crash the orchestrator; its printed metric is parsed back.
        rel = spec.get("path", "LOOPLAB_adapter.py")
        if not (Path(workdir) / rel).is_file():
            return None
        runner = ("import json, runpy; "
                  f"_ns = runpy.run_path({rel!r}); "
                  "print(json.dumps({'metric': _ns['read_metric']('.')}))")
        # In the container use its `python` (the host sys.executable path doesn't exist there);
        # locally use the same interpreter that runs the engine.
        argv = (["python", "-c", runner] if wrap else [sys.executable, "-c", runner])
        if wrap:
            argv = wrap(argv, str(workdir))
        rc, out, _, to = _run_argv(argv, str(workdir),
                                   float(spec.get("timeout", 120)), None, 64_000)
        return _json_line_metric(out, "metric") if (rc == 0 and not to) else None
    return None


def _fmt(v) -> str:
    """Format a param value for a CLI override: integral floats as ints (epochs=50, not
    epochs=50.0), everything else as-is."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else repr(f)


def build_command(eval_spec: dict, params: Optional[dict] = None,
                  profile: Optional[str] = None) -> tuple[list[str], float]:
    """Build the eval argv + timeout from an eval_spec, an eval profile (smoke/full), and
    the Researcher's params (RepoTask Phase 2). Returns (command, timeout).

    - profiles: named override sets, e.g. {"smoke": {"overrides": ["max_steps=20"],
      "timeout": 60}}. `profile=None` (search default) resolves to "smoke"; an explicitly
      REQUESTED name that isn't defined uses NO overrides (the base/full command), never a
      cheaper fallback — so confirm("full") can't silently run the smoke eval.
    - params_style == "cli_overrides": the proposed params are appended as `key=value`
      tokens (Hydra-style), so hyperparameters drive an existing framework with no code edit.
    """
    cmd = list(eval_spec["command"])
    profiles = eval_spec.get("profiles") or {}
    # Resolve the profile. An explicitly-requested name that isn't defined uses NO overrides
    # (the base/full command) — never a cheaper fallback, so confirm("full") can't silently
    # run the smoke eval. profile=None means "search default" -> the conventional "smoke".
    prof = profiles.get(profile) if profile else profiles.get("smoke")
    overrides = list((prof or {}).get("overrides", []))
    # Explicit presence check (not `or`) so a configured timeout of 0 isn't read as missing.
    timeout = prof["timeout"] if (prof and "timeout" in prof) else eval_spec.get("timeout", 600.0)
    if eval_spec.get("params_style") == "cli_overrides":
        overrides += [f"{k}={_fmt(v)}" for k, v in (params or {}).items()]
    return cmd + overrides, timeout


_CROSS_CHECK_ADAPTER_MSG = ("cross_check must be an independent built-in reader, not "
                            "'adapter' (an agent-authored cross-check defeats the purpose).")


def validate_cross_check(spec: Optional[dict]) -> Optional[dict]:
    """The drift cross_check must be a declarative built-in reader, never agent-authored
    `adapter` code. One predicate used by both EvalSpec validation and the runtime guard."""
    if spec is not None and spec.get("kind") == "adapter":
        raise ValueError(_CROSS_CHECK_ADAPTER_MSG)
    return spec


def _drift(primary: Optional[float], cross: Optional[float], tol: float) -> bool:
    """True if the frozen adapter's `primary` metric is not corroborated by the independent
    `cross` reader: either the cross reader produced nothing (can't confirm) or the two
    diverge beyond `tol` (relative+absolute, so it scales with the metric's magnitude).
    Only called when there IS a primary to corroborate."""
    import math
    if cross is None or not math.isfinite(primary) or not math.isfinite(cross):
        return True                                    # can't corroborate -> drift (defense)
    return abs(primary - cross) > tol * (1.0 + abs(cross))


def make_docker_wrap(mount_root: str, image: str, network: str = "none",
                     mem: Optional[str] = None):
    """untrusted tier (ADR-13, Phase 4): return a `wrap(argv, host_cwd) -> argv` that runs the
    command inside `docker run` with the run workspace bind-mounted at /work and the network
    off by default — a real isolation boundary for executing an arbitrary framework. The bind
    mount means files the container writes (metrics, logs) appear on the host, so metric
    reading still happens on host paths afterward. Fails LOUDLY if the docker CLI is absent
    rather than silently running unsandboxed (mirrors sandbox.DockerSandbox)."""
    import shutil as _sh
    if not _sh.which("docker"):
        raise RuntimeError(
            "trust_mode='untrusted' needs the docker CLI to sandbox the eval, but it was not "
            "found on PATH. Install Docker or use trust_mode='trusted_local'.")
    root = Path(mount_root).resolve()

    def wrap(argv: list[str], host_cwd: str) -> list[str]:
        rel = os.path.relpath(Path(host_cwd).resolve(), root).replace(os.sep, "/")
        if rel == ".." or rel.startswith("../"):     # cwd outside the mounted root -> never escape
            raise ValueError(f"eval cwd {host_cwd!r} is outside the mounted workspace {str(root)!r}")
        cdir = "/work" if rel in (".", "") else f"/work/{rel}"
        base = ["docker", "run", "--rm", "--network", network,
                "-v", f"{root.as_posix()}:/work", "-w", cdir]
        if mem:
            base += ["--memory", mem]
        return base + [image] + list(argv)

    wrap._docker = True   # marks a real container wrap -> run_command_eval adds in-container timeout
    return wrap


def _violations(out, wd, constraints, wrap) -> list[dict]:
    """Read each constraint (a reader spec + a `max`/`min` bound) and return the ones not
    satisfied (incl. a value that couldn't be read — an unverifiable constraint is a
    violation, never a silent pass). Multi-objective gate (#2/#5): a violating node is still
    measured but excluded from best-selection."""
    out_list = []
    for c in (constraints or []):
        val = read_metric(out, wd, c, wrap=wrap)
        bad = (val is None
               or (c.get("max") is not None and val > c["max"])
               or (c.get("min") is not None and val < c["min"]))
        if bad:
            out_list.append({"name": c.get("name", "constraint"), "value": val,
                             "max": c.get("max"), "min": c.get("min")})
    return out_list


def run_command_eval(command: list[str], cwd: str, timeout: float, metric: dict,
                     env: Optional[dict] = None, max_output_bytes: int = 64_000,
                     setup: Optional[list] = None, setup_timeout: float = 600.0,
                     setup_cwd: Optional[str] = None, cross_check: Optional[dict] = None,
                     drift_tolerance: float = 1e-6, enforce_drift: bool = False,
                     wrap=None, metrics: Optional[dict] = None,
                     constraints: Optional[list] = None, tracer=None) -> RunResult:
    """Run `command` (argv, no shell) in `cwd`, capped + timeout + tree-kill, then read the
    metric. If `setup` is given (e.g. a dependency install), it runs FIRST in `setup_cwd`
    (defaults to the repo/workdir root, NOT the eval `cwd` subdir — so a root-level
    requirements file is reachable); a non-zero/timed-out setup short-circuits to a failed
    RunResult (its stderr is the error fed back to the Developer's repair).

    Drift cross-check (Phase 4): when `enforce_drift` and a `cross_check` reader are given,
    the metric is read a SECOND time via that independent (declarative, never `adapter`)
    reader; if it can't corroborate the primary within `drift_tolerance`, the metric is
    discarded (set to None) and `RunResult.drift` records the divergence. This catches a
    metric faked through the eval workdir even when the adapter file itself is frozen.

    `wrap` (untrusted tier): a `wrap(argv, host_cwd) -> argv` from `make_docker_wrap` that
    runs each command inside a container. The host cwd is still passed to the subprocess (the
    docker CLI ignores it); metric reading stays on host paths via the bind mount.
    Returns the sandbox `RunResult` shape."""
    wd = Path(cwd).resolve()
    wd.mkdir(parents=True, exist_ok=True)
    _w = (lambda argv, hc: wrap(argv, hc)) if wrap else (lambda argv, hc: argv)

    def _sp(name, **attrs):                              # child span when a tracer is wired
        return tracer.span(name, **attrs) if tracer is not None else nullcontext(None)

    # Only a REAL docker wrap gets the in-container `timeout` prefix (a non-docker passthrough
    # wrap, e.g. in tests, must not get a host `timeout` prepended — that is timeout.exe on
    # Windows and would break the command).
    is_docker = bool(getattr(wrap, "_docker", False))

    def _bound(argv, secs):
        # Under the docker wrap, self-limit the container with coreutils `timeout` so a runaway
        # exits from INSIDE (+ --rm cleanup) even if the host kills the `docker run` client —
        # killing the CLI does not stop the daemon-owned container.
        return (["timeout", "-k", "5", str(max(1, int(secs)))] + list(argv)) if is_docker else list(argv)

    grace = 15.0 if is_docker else 0.0
    if setup:
        swd = Path(setup_cwd).resolve() if setup_cwd else wd
        swd.mkdir(parents=True, exist_ok=True)
        with _sp("setup", sandboxed=bool(wrap)):
            rc, out, err, to = _run_argv(_w(_bound(setup, setup_timeout), str(swd)), swd,
                                         setup_timeout + grace, env, max_output_bytes)
        to = to or (is_docker and rc == 124)            # coreutils timeout -> exit 124
        if rc != 0 or to:
            return RunResult(exit_code=rc, stdout=out, stderr="setup failed:\n" + err,
                             metric=None, timed_out=to)
    with _sp("command", sandboxed=bool(wrap)) as _h:
        rc, out, err, to = _run_argv(_w(_bound(command, timeout), str(wd)), wd,
                                     timeout + grace, env, max_output_bytes)
        to = to or (is_docker and rc == 124)
        if _h is not None:
            _h.set_many(exit_code=rc, timed_out=to)
    with _sp("read_metric", kind=metric.get("kind", "stdout_json")):
        m = read_metric(out, str(wd), metric, wrap=wrap) if not to else None
    drift = None
    if enforce_drift and cross_check and m is not None:
        validate_cross_check(cross_check)
        cross = read_metric(out, str(wd), cross_check, wrap=wrap)
        if _drift(m, cross, drift_tolerance):
            drift = {"primary": m, "cross": cross, "tolerance": drift_tolerance}
            m = None                                   # uncorroborated -> not trusted
    # Multi-objective (#5): extra reported metrics (audit) + hard constraints (gate selection).
    # These reader specs are operator-owned gates, so they must NOT be agent-authored `adapter`
    # code (same trust rule as cross_check) — reject loudly rather than exec the agent's module.
    for spec in list((metrics or {}).values()) + list(constraints or []):
        if spec.get("kind") == "adapter":
            raise ValueError("metrics/constraints readers must be built-in, not 'adapter' "
                             "(an agent-authored gate reader defeats the trust boundary).")
    extra = ({name: read_metric(out, str(wd), spec, wrap=wrap)
              for name, spec in metrics.items()} if (metrics and not to) else None)
    viol = _violations(out, str(wd), constraints, wrap) if (constraints and not to and m is not None) else None
    return RunResult(exit_code=rc, stdout=out, stderr=err, metric=m, timed_out=to, drift=drift,
                     extra_metrics=extra, violations=(viol or None))


