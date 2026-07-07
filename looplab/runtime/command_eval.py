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
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Optional

from looplab.runtime.sandbox import (RunResult, _to_float, docker_timed_out, json_line_extras,
                                     json_line_metric, json_line_trials, run_argv)


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
        return json_line_metric(stdout, spec.get("key", "metric"))
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
    if kind == "host_score":
        # B1 host-side scoring (trust): the candidate WRITES predictions into its workdir; the HOST
        # scores them against held-out labels it holds at a path OUTSIDE the candidate's workspace
        # (never mounted under the untrusted tier, never writable by the candidate). The metric is
        # computed here, on the host — the candidate cannot self-report or see the labels. This turns
        # `stdout_json` self-reporting into an enforced guarantee for untrusted real tasks.
        preds_path = Path(workdir) / spec.get("predictions", "predictions.json")
        labels_path = Path(spec["labels"]).resolve()   # operator-declared host path (trusted)
        # Enforce the invariant the docstring asserts: the answer key must live OUTSIDE the
        # candidate's workspace. Under the untrusted/hostile tier the whole MOUNT ROOT (the run root)
        # is bind-mounted into the container — not just the eval cwd — so a labels path anywhere under
        # the mount root is readable AND writable by the candidate, defeating held-out grading. Guard
        # against the mounted root when a docker wrap is active (it's a strict superset of the cwd);
        # fall back to the cwd otherwise. Fail loud on misconfig.
        guard_root = Path(workdir).resolve()
        mount_root = getattr(wrap, "_mount_root", None) if wrap is not None else None
        if mount_root:
            guard_root = Path(mount_root).resolve()
        if _is_within(labels_path, guard_root):
            raise ValueError(
                f"host_score labels path {labels_path} is inside the candidate workspace "
                f"{guard_root} — it would be mounted/writable by the candidate. "
                "Place the held-out labels outside the eval workspace.")
        if not preds_path.is_file() or not labels_path.is_file():
            return None
        try:
            preds = json.loads(preds_path.read_text(encoding="utf-8-sig", errors="replace"))
            labels = json.loads(labels_path.read_text(encoding="utf-8-sig", errors="replace"))
        except (json.JSONDecodeError, OSError):
            return None
        return _to_float(host_score(spec.get("scorer", "rmse"), preds, labels, key=spec.get("key")))
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
        rc, out, _, to = run_argv(argv, str(workdir),
                                   float(spec.get("timeout", 120)), None, 64_000)
        return json_line_metric(out, "metric") if (rc == 0 and not to) else None
    return None


def _is_within(child: Path, parent: Path) -> bool:
    """True if `child` is `parent` or nested under it (both already resolved)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# Default keys probed when coercing a dict payload to a list. The CANDIDATE-controlled
# predictions payload is restricted to a single canonical key so a candidate can't ship
# several arrays and let key-precedence pick the most favorable one (it must be a bare list
# or live under the explicit `key`/"predictions"). The host-held labels keep the full set.
_PRED_KEYS = ("predictions",)
_LABEL_KEYS = ("predictions", "preds", "y", "labels", "values")


def _as_list(obj, key: Optional[str], fallbacks: tuple[str, ...] = _LABEL_KEYS):
    """Coerce a predictions/labels payload to a flat list: a bare list, or `obj[key]` (e.g.
    {"predictions": [...]}), or the first of `fallbacks` present in the dict."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        if key and key in obj:
            return obj[key]
        for cand in fallbacks:
            if cand in obj:
                return obj[cand]
    return None


def _label_eq(a, b) -> bool:
    """Discrete-label equality for accuracy/error_rate. Treats numerically-equal encodings
    as equal (int 1 == float 1.0 == str "1") so a JSON-stringified or float-encoded class
    label still matches, while a genuine non-label value (a probability 0.999) stays unequal."""
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def host_score(scorer: str, preds, labels, *, key: Optional[str] = None) -> Optional[float]:
    """B1: compute a metric on the HOST from candidate predictions + held-out labels. Built-in,
    dependency-free scorers (data, never agent code). Returns None on shape/empty mismatch."""
    yp = _as_list(preds, key, _PRED_KEYS)            # candidate payload: no key-shopping
    yt = _as_list(labels, key, _LABEL_KEYS)          # host labels: full fallback set
    if not isinstance(yp, list) or not isinstance(yt, list) or not yt or len(yp) != len(yt):
        return None
    try:
        if scorer in ("rmse", "mse", "mae"):
            errs = [(float(a) - float(b)) for a, b in zip(yp, yt)]
            if scorer == "mae":
                return sum(abs(e) for e in errs) / len(errs)
            mse = sum(e * e for e in errs) / len(errs)
            return mse if scorer == "mse" else mse ** 0.5
        if scorer in ("accuracy", "acc"):
            return sum(1 for a, b in zip(yp, yt) if _label_eq(a, b)) / len(yt)
        if scorer == "error_rate":
            return 1.0 - sum(1 for a, b in zip(yp, yt) if _label_eq(a, b)) / len(yt)
    except (TypeError, ValueError):
        return None
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
                     mem: Optional[str] = None, runtime: Optional[str] = None):
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
        rt = ["--runtime", runtime] if runtime else []   # B4+ gVisor/Kata true-isolation tier
        base = ["docker", "run", "--rm", "--network", network, *rt,
                "--pids-limit", "1024",       # fork-bomb guard (review C1: no pids limit before)
                "-v", f"{root.as_posix()}:/work", "-w", cdir]
        if mem:
            base += ["--memory", mem]
        return base + [image] + list(argv)

    wrap._docker = True   # marks a real container wrap -> run_command_eval adds in-container timeout
    wrap._mount_root = str(root)   # host_score guards the held-out labels against the MOUNTED root
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
                     constraints: Optional[list] = None, tracer=None, cancel=None,
                     log_dir: Optional[str] = None,
                     stages: Optional[list] = None,
                     start_stage: Optional[str] = None,
                     check_fn=None) -> RunResult:
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
    # Live, tail-able logs of the setup + eval subprocesses (e.g. training epochs), so a long
    # eval isn't opaque until it returns. None -> buffered fast path (unchanged).
    _log = lambda name: (str(Path(log_dir) / name) if log_dir else None)

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
            rc, out, err, to = run_argv(_w(_bound(setup, setup_timeout), str(swd)), swd,
                                         setup_timeout + grace, env, max_output_bytes, cancel,
                                         log_path=_log("setup.log"))
        to = to or (is_docker and docker_timed_out(rc))   # coreutils timeout -> exit 124 or 137
        if rc != 0 or to:
            return RunResult(exit_code=rc, stdout=out, stderr="setup failed:\n" + err,
                             metric=None, timed_out=to)
    stage_results = None
    if stages:
        # Multi-stage pipeline (data_prep → train → eval): run each stage in ORDER in the SAME workdir
        # (artifacts persist across stages), each in its own span + <name>.log, tracking pass/fail. The
        # FIRST failure stops the pipeline and returns "failed at stage <name>" — so a crash in `train`
        # is pinpointed (not hidden behind an opaque single command) and the good earlier stages' outputs
        # stay on disk for a later stage-scoped re-run. The LAST stage's stdout carries the metric.
        # Stage-scoped re-run (Phase 2): `start_stage` re-runs the pipeline FROM that stage, reusing the
        # earlier stages' on-disk artifacts (the checkpoint `train` wrote survives in the workdir). So a
        # crashed `eval` is fixed without paying to re-`train`. Stages before it are marked "reused".
        _run_from = 0
        if start_stage:
            for _i, _s in enumerate(stages):
                if str(_s.get("name")) == str(start_stage):
                    _run_from = _i
                    break
        stage_results = []
        for _i, _stg in enumerate(stages):
            _sname = str(_stg.get("name") or f"stage{_i}")
            _scmd = list(_stg.get("command") or [])
            if _i < _run_from:
                stage_results.append({"name": _sname, "status": "reused", "exit_code": 0, "seconds": 0.0})
                continue
            _sto = float(_stg.get("timeout", timeout))
            if not _scmd:
                continue
            _t0 = time.monotonic()
            with _sp(_sname, kind="operation", sandboxed=bool(wrap), stage=_sname) as _sh:
                rc, out, err, to = run_argv(_w(_bound(_scmd, _sto), str(wd)), wd,
                                            _sto + grace, env, max_output_bytes, cancel,
                                            log_path=_log(f"{_sname}.log"))
                to = to or (is_docker and docker_timed_out(rc))
                if _sh is not None:
                    _sh.set_many(exit_code=rc, timed_out=to, stage=_sname)
            _status = "timeout" if to else ("ok" if rc == 0 else "fail")
            stage_results.append({"name": _sname, "status": _status, "exit_code": rc,
                                  "seconds": round(time.monotonic() - _t0, 3)})
            if _status != "ok":
                return RunResult(exit_code=rc, stdout=out, stderr=f"stage '{_sname}' failed:\n{err}",
                                 metric=None, timed_out=to, stages=stage_results, failed_stage=_sname)
            # Phase 3 — optional inter-stage verify: a stage flagged `"check": true` hands its output tail
            # to an agentic checker (Researcher/Developer) BEFORE the next stage runs; a returned concern
            # stops the pipeline early ("failed verification") so a bad artifact (e.g. a diverged train)
            # doesn't silently feed the next stage. No check_fn / no flag => never called (zero overhead).
            if _stg.get("check") and check_fn is not None:
                try:
                    _concern = check_fn(_sname, out[-4000:])
                except Exception:  # noqa: BLE001 — a checker failure must not crash the eval
                    _concern = None
                if _concern:
                    stage_results[-1]["status"] = "check_failed"
                    stage_results[-1]["concern"] = str(_concern)[:300]
                    return RunResult(exit_code=0, stdout=out, metric=None, timed_out=False,
                                     stderr=f"stage '{_sname}' failed verification: {_concern}",
                                     stages=stage_results, failed_stage=_sname)
        # all stages passed -> the LAST stage's `out`/`rc`/`to` flow into read_metric below.
    else:
        with _sp("command", sandboxed=bool(wrap)) as _h:
            rc, out, err, to = run_argv(_w(_bound(command, timeout), str(wd)), wd,
                                         timeout + grace, env, max_output_bytes, cancel,
                                         log_path=_log("eval.log"))
            to = to or (is_docker and docker_timed_out(rc))   # 124 (SIGTERM) or 137 (SIGKILL escalation)
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
    declared = ({name: v for name, spec in metrics.items()
                 if (v := read_metric(out, str(wd), spec, wrap=wrap)) is not None}
                if (metrics and not to) else {})   # a MISSED reader (None) must not erase a
    #                                                successfully auto-captured value of the same name
    # Auto-capture: every other numeric key on the metric's own JSON line is also reported (no config
    # needed), so an experiment that prints {"metric": x, "recall@10": y, ...} surfaces them all. A
    # declared spec wins over the auto-captured value of the same name.
    auto = (json_line_extras(out, metric.get("key", "metric"))
            if (not to and metric.get("kind", "stdout_json") == "stdout_json") else {})
    extra = ({**auto, **declared} or None)
    viol = _violations(out, str(wd), constraints, wrap) if (constraints and not to and m is not None) else None
    # Intra-node sweep: a RepoTask command may emit the same `{"trials": [...]}` stdout line; carry
    # it so the engine can collapse it to the node's best metric (no eval_spec change required).
    trials = json_line_trials(out) if not to else None
    return RunResult(exit_code=rc, stdout=out, stderr=err, metric=m, timed_out=to, drift=drift,
                     extra_metrics=extra, violations=(viol or None), trials=trials,
                     stages=stage_results)


