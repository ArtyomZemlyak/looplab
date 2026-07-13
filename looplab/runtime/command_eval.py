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
import posixpath
import re
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Optional

from looplab.runtime.sandbox import (RunResult, _to_float, docker_timed_out, finite_timeout,
                                     json_line_extras, json_line_metric, json_line_trials, run_argv)

# A stage name is interpolated into a log FILE PATH (`<name>.log`) and shown in the trace, so it must
# be a short filesystem-safe SLUG — no path separators, drive letters, control chars, NUL, or dot
# segments (`.`/`..`). Without this a stage named `../escape` (or `C:\x`, or an embedded NUL that
# raises ValueError only AFTER the child spawned) writes/redirects its log outside the run dir
# (arch-review §3 P0-7). The allowlist is deliberately strict: an alnum start, then alnum/_/-/.  with
# no `..` anywhere, bounded length. Both authoring (declare_stages) and consume (EvalSpec.stages)
# validate through validate_stages, so a bad name can never reach the runner.
# `\Z` (not `$`): Python's `$` also matches just BEFORE a trailing newline, so `$` would accept
# `"train\n"` — a control char the "filesystem-safe slug" contract promises to reject (it would land
# in a log filename and trace/span attributes). `\Z` anchors the true end of string.
_STAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def safe_stage_name(name: str) -> bool:
    """True when `name` is a filesystem-safe stage slug (see `_STAGE_NAME_RE`): rejects separators,
    drives, control/NUL bytes, and `.`/`..` dot segments that would escape the log directory."""
    return bool(name) and ".." not in name and bool(_STAGE_NAME_RE.match(name))


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
        rx = re.compile(pattern)

        def _last(s):
            last = None
            for m in rx.finditer(s):   # take the LAST match in this window (final epoch, etc.)
                last = m
            return last

        # Bound the input: an operator/agent-authored pattern run over the FULL stdout can ReDoS-hang
        # the engine thread on a pathological regex. The metric is usually at the TAIL (final epoch),
        # so scan the last ~200k chars first; but a script that prints the metric EARLY and then dumps
        # a long report/prediction log would lose it to a tail-only cap, so fall back to the HEAD 200k
        # when the tail has no match. Bounded to 2×200k either way — the ReDoS ceiling is preserved.
        if len(text) > 200_000:
            last = _last(text[-200_000:]) or _last(text[:200_000])
        else:
            last = _last(text)
        return _to_float(last.group(group)) if last else None
    except (re.error, IndexError):
        return None


# Freshness-gate slack (seconds): allow a metric file whose mtime is up to this far BEFORE the eval
# start to still count as fresh, absorbing coarse filesystem mtime granularity (some mounts floor to
# 1s) + minor clock skew. Small vs. the real staleness case (a prior attempt's artifact is seconds-to-
# minutes old, well past this), so it never lets a genuinely stale file through.
_FRESH_EPS = 2.0


def _file_is_fresh(p: Path, since: Optional[float]) -> bool:
    """True if the metric-source file `p` was (re)written by the CURRENT eval — its mtime is at/after
    the eval start (minus _FRESH_EPS). `since=None` disables the gate (non-eval / legacy callers).
    Guards the workdir-reuse trap: a successful-looking command that produced NO new output would else
    let a STALE prior-attempt artifact (predictions/metrics file lingering in a coarsely-keyed, un-
    cleaned workdir) be read as this eval's result and promote a false metric (arch-review §6.3)."""
    if since is None:
        return True
    try:
        return p.stat().st_mtime >= since - _FRESH_EPS
    except OSError:
        return False


def read_metric(stdout: str, workdir: str, spec: dict, wrap=None,
                since: Optional[float] = None) -> Optional[float]:
    """Read the metric for one eval according to `spec` (an eval_spec['metric']). Built-in
    readers parse host files/stdout in-process (data, never code). The `adapter` reader EXECS
    agent-authored code, so under the untrusted tier it must run in the same sandbox as the
    eval — pass `wrap` (from make_docker_wrap) to run it inside the container.

    `since` (eval start time, from run_command_eval): FILE-based readers reject a metric-source file
    older than it — a stale artifact left in a reused workdir must not read as this eval's result."""
    kind = spec.get("kind", "stdout_json")
    if kind == "stdout_json":
        return json_line_metric(stdout, spec.get("key", "metric"))
    if kind == "stdout_regex":
        pat = spec.get("pattern") or spec.get("key")   # key = tolerant fallback (composable authoring)
        return _regex_metric(stdout, pat, int(spec.get("group", 1))) if pat else None
    if kind in ("file_json", "file_regex"):
        fp = spec.get("path")
        if not fp:
            return None                                 # malformed spec must fail the NODE, not crash the run
        p = Path(workdir) / fp
        # Confine the reader to the workdir (same guard as host_score's held-out-labels check): an
        # absolute `path` or a `../` traversal in the spec would otherwise escape the sandbox and read
        # any host file (a direct answer-key read the moment reader paths become agent-authorable).
        wd = Path(workdir)
        try:
            if not _is_within(p.resolve(), wd.resolve()):
                return None
        except (OSError, ValueError):
            return None
        if not p.is_file():
            return None
        if not _file_is_fresh(p, since):
            return None                                 # stale prior-attempt file in a reused workdir
        # utf-8-sig strips a UTF-8 BOM (common on Windows-written metric files) that would
        # otherwise make json.loads fail / regex miss the first line.
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        if kind == "file_regex":
            pat = spec.get("pattern") or spec.get("key")
            return _regex_metric(text, pat, int(spec.get("group", 1))) if pat else None
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
        # Contain the CANDIDATE-controlled predictions path INSIDE the workdir. A `../preds.json` (or an
        # absolute path, or a symlink out) would read a stale/planted file outside the attempt workspace
        # and could return a perfect score (arch-review §3 P0-7). Labels are guarded to be OUTSIDE the
        # workspace below; predictions must be INSIDE it. `.resolve()` also collapses a symlink escape.
        try:
            if not _is_within(preds_path.resolve(), Path(workdir).resolve()):
                return None
        except (OSError, ValueError):
            return None
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
        if not _file_is_fresh(preds_path, since):
            # The candidate's predictions must be written by THIS eval — a stale predictions.json from a
            # prior attempt in a reused workdir could otherwise score as a perfect result (false promo).
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
        ap = Path(workdir) / rel
        # Contain the adapter module INSIDE the workdir before EXECing it: an absolute or `../` path
        # (or a symlink out) would runpy an arbitrary host .py — a code-exec escape the file readers
        # already guard but this branch did not (arch-review §3 P0-7). `.resolve()` collapses symlinks.
        try:
            if not _is_within(ap.resolve(), Path(workdir).resolve()) or not ap.is_file():
                return None
        except (OSError, ValueError):
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
                                   finite_timeout(spec.get("timeout", 120), 120), None, 64_000)
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


def expand_params(argv: list, params: Optional[dict]) -> list:
    """Substitute a standalone `%params%` token in an argv list with the node's params rendered as
    `--key value` items (integral floats as ints). This is the EXPLICIT, opt-in way hyperparameters
    reach a command or a pipeline stage: the Developer/operator writes `%params%` exactly where the
    flags belong. No `%params%` token -> the argv is returned unchanged (params are baked into the
    code by the Developer, or the run doesn't tune params). Passing no params just drops a stray
    token. Used for both the single `cmd` and each stage command."""
    if "%params%" not in argv:
        return list(argv)
    out = []
    for a in argv:
        if a == "%params%":
            for k, v in (params or {}).items():
                out.append(f"--{k}")
                out.append(_fmt(v))
        else:
            out.append(a)
    return out


def validate_stages(stages, *, reserved: tuple = ()) -> tuple[Optional[list], Optional[str]]:
    """Validate a stage list ({name, command:[argv...], timeout?, check?}) into its canonical clean
    form: (clean, None) on success, (None, reason) on the first problem. This is the SINGLE
    definition of "a valid stage", shared by the Developer's `declare_stages` tool (authoring time),
    `EvalSpec.stages` (the operator's cmd pipeline, submit time) and the engine's `_resolve_stages`
    (consume time) — so the two ends of the manifest handshake can't drift: a stage one side accepts
    is never silently dropped or re-interpreted by the other. `reserved` names are refused — the
    engine appends the operator's protected `score` stage to a DEVELOPER manifest, so the tool passes
    ("score",); operator-declared stages reserve nothing (the operator owns scoring)."""
    if not isinstance(stages, list) or not stages:
        return None, "`stages` must be a non-empty array of {name, command:[argv...]} objects."
    seen, clean = set(), []
    for i, s in enumerate(stages):
        if not isinstance(s, dict):
            return None, f"stage {i} is not an object — expected {{name, command:[...]}}."
        nm = str(s.get("name") or "").strip()
        if not nm:
            return None, f"stage {i} has no `name`."
        if not safe_stage_name(nm):
            # The name becomes a `<name>.log` path + a trace label — keep it a filesystem-safe slug so
            # it can't traverse out of the log dir (`../escape`, `C:\x`, control/NUL). See safe_stage_name.
            return None, (f"stage name {nm!r} must be a short slug (letters, digits, '_', '-', '.'; "
                          "no path separators, drive letters, control characters, or '..').")
        if nm.lower() in reserved:
            return None, ("'score' is reserved for the operator's final scoring stage. Name "
                          "your PRECEDING stages e.g. data_prep, train — the cmd is appended after them.")
        if nm in seen:
            return None, f"duplicate stage name {nm!r}."
        seen.add(nm)
        cmd = s.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            return None, (f"stage {nm!r} needs a `command` as a non-empty list of string argv "
                          "items, e.g. [\"python\",\"train.py\",\"%params%\"].")
        st = {"name": nm, "command": list(cmd)}
        if "timeout" in s:
            try:
                _t = float(s["timeout"])
            except (TypeError, ValueError):
                return None, f"stage {nm!r} `timeout` must be a number of seconds."
            import math
            if not math.isfinite(_t) or _t <= 0:
                # A NaN/inf/negative/zero stage timeout would disable (or trivially trip) the wall-clock
                # deadline — reject at authoring time rather than coerce silently (arch-review §4 P1-5).
                return None, f"stage {nm!r} `timeout` must be a finite, positive number of seconds."
            st["timeout"] = _t
        if s.get("check"):
            st["check"] = True
        clean.append(st)
    return clean, None


def materialized_stages(manifest_obj, *, reserved: tuple = ("score",)) -> Optional[list]:
    """The validated PRECEDING stage list from a PARSED `looplab_stages.json` object, or None when it
    declares no usable pipeline / fails validation. Accepts BOTH the wrapped ``{"stages":[...]}`` shape
    `declare_stages` authors AND a bare top-level JSON list (hand-written / write_file / pre-redesign
    manifests): ``dev = obj.get("stages") if isinstance(obj, dict) else obj``. The SINGLE source of
    truth for reading a materialized dev manifest, shared by the eval's `_resolve_stages` (consume
    time) and the repo-Developer's implement-prompt `_materialized_stage_list` (authoring time) — so
    the two can't drift and advertise a pipeline different from the one the eval runs (M7). An invalid
    manifest the eval would DROP to the single command returns None here too."""
    dev = manifest_obj.get("stages") if isinstance(manifest_obj, dict) else manifest_obj
    if not isinstance(dev, list) or not dev:
        return None
    clean, err = validate_stages(dev, reserved=reserved)
    return clean if (err is None and clean) else None


def build_command(eval_spec: dict, params: Optional[dict] = None,
                  profile: Optional[str] = None) -> tuple[list[str], float]:
    """Build the eval argv + timeout from an eval_spec, an eval profile (smoke/full), and
    the node's params. Returns (command, timeout).

    - profiles: named override sets, e.g. {"smoke": {"overrides": ["max_steps=20"],
      "timeout": 60}}. `profile=None` (search default) resolves to "smoke"; an explicitly
      REQUESTED name that isn't defined uses NO overrides (the base/full command), never a
      cheaper fallback — so confirm("full") can't silently run the smoke eval.
    - params reach the command via a `%params%` token (see `expand_params`) — the explicit,
      composable-schema way. Legacy `params_style == "cli_overrides"` still appends `key=value`
      tokens (Hydra-style) for old tasks that set it.
    """
    _argv = list(eval_spec["command"])
    _had_token = "%params%" in _argv
    cmd = expand_params(_argv, params)                        # %params% token -> --key value
    profiles = eval_spec.get("profiles") or {}
    # Resolve the profile. An explicitly-requested name that isn't defined uses NO overrides
    # (the base/full command) — never a cheaper fallback, so confirm("full") can't silently
    # run the smoke eval. profile=None means "search default" -> the conventional "smoke".
    prof = profiles.get(profile) if profile else profiles.get("smoke")
    overrides = list((prof or {}).get("overrides", []))
    # Explicit presence check (not `or`) so a configured timeout of 0 isn't read as missing.
    timeout = prof["timeout"] if (prof and "timeout" in prof) else eval_spec.get("timeout", 600.0)
    if eval_spec.get("params_style") == "cli_overrides" and not _had_token:  # legacy Hydra append …
        overrides += [f"{k}={_fmt(v)}" for k, v in (params or {}).items()]   # … skip if %params% already injected
    # Bound the timeout to a finite, positive, capped value: a NaN/inf here would flow into the
    # deadline (never fires) and into the docker `timeout -k` prefix as int(nan) (ValueError). P1-5.
    return cmd + overrides, finite_timeout(timeout, 600.0)


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
                     mem: Optional[str] = None, cpus: Optional[str] = None,
                     runtime: Optional[str] = None, binds: Optional[list] = None,
                     env: Optional[dict] = None):
    """untrusted tier (ADR-13, Phase 4): return a `wrap(argv, host_cwd) -> argv` that runs the
    command inside `docker run` with the run workspace bind-mounted at /work and the network
    off by default — a real isolation boundary for executing an arbitrary framework. The bind
    mount means files the container writes (metrics, logs) appear on the host, so metric
    reading still happens on host paths afterward. Fails LOUDLY if the docker CLI is absent
    rather than silently running unsandboxed (mirrors sandbox.DockerSandbox).

    `binds`: extra (host_path, read_only) mounts, bound at the SAME absolute path inside the
    container. Symlink-mounted data/reference sources need this — the /work bind carries only the
    symlink, which would otherwise dangle in the container — and binding a non-editable source
    `:ro` is the MOUNT-LAYER enforcement of the per-source `edit:false` permission: code running
    in the sandbox physically cannot write the operator's original, matching the write-tool gate
    (mega-review fix; the write gate alone couldn't stop a declared train stage from mutating the
    original through ./<name>)."""
    import shutil as _sh
    if not _sh.which("docker"):
        raise RuntimeError(
            "trust_mode='untrusted' needs the docker CLI to sandbox the eval, but it was not "
            "found on PATH. Install Docker or use trust_mode='trusted_local'.")
    root = Path(mount_root).resolve()
    extra: list[str] = []
    for p, ro in (binds or []):
        raw = os.fspath(p)
        # A task may deliberately carry a POSIX-absolute source path even when the Docker
        # client runs on Windows.  WindowsPath.resolve() treats `/data/raw` as rooted on the
        # current drive and silently rewrites it to `C:/data/raw`, so the same-path mount no
        # longer matches the symlink target inside the Linux container.  Preserve that path
        # dialect (while lexically normalizing `.`/`..`); drive/UNC and relative host paths
        # still go through the native resolver as before.
        if os.name == "nt" and raw.startswith("/") and not raw.startswith("//"):
            ap = posixpath.normpath(raw)
        else:
            ap = Path(raw).resolve().as_posix()
        # Use `--mount` (comma-separated key=value) instead of `-v host:container[:ro]`: the colon form
        # is MALFORMED for a resolved Windows host path — `C:/data:C:/data:ro` -> Docker Desktop "too
        # many colons" — so untrusted Windows RepoTasks with extra inputs failed (arch-review §4 P1-8).
        # `--mount` parses src/dst as explicit keys with no colon ambiguity. dst mirrors the symlink
        # target (same absolute path) so the in-/work symlink resolves in the container. Only real
        # symlinks reach here (see _data_binds), so on POSIX ap is a valid Linux container path; a
        # copied-in source (the common Windows case) rides in the /work bind and is never bound here.
        spec = f"type=bind,src={ap},dst={ap}" + (",readonly" if ro else "")
        extra += ["--mount", spec]

    def wrap(argv: list[str], host_cwd: str) -> list[str]:
        rel = os.path.relpath(Path(host_cwd).resolve(), root).replace(os.sep, "/")
        if rel == ".." or rel.startswith("../"):     # cwd outside the mounted root -> never escape
            raise ValueError(f"eval cwd {host_cwd!r} is outside the mounted workspace {str(root)!r}")
        cdir = "/work" if rel in (".", "") else f"/work/{rel}"
        rt = ["--runtime", runtime] if runtime else []   # B4+ gVisor/Kata true-isolation tier
        # Resource + privilege hardening for the untrusted tier — mirror sandbox.DockerSandbox.run
        # so BOTH untrusted Docker tiers (this command-eval path and the solution.py path) bound
        # memory/cpu, drop all Linux capabilities, and forbid privilege escalation. Without this an
        # untrusted/hostile RepoTask candidate keeps default caps (setuid escalation) and can OOM the
        # host / saturate every core (gVisor blocks kernel escape, NOT resource exhaustion). The two
        # docs (generating-code.md untrusted-tier command, configuration.md sandbox_memory/cpus rows)
        # promise exactly these flags for this tier — the engine now threads settings.sandbox_* here.
        caps = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        if mem:
            caps += ["--memory", str(mem)]
        if cpus:
            caps += ["--cpus", str(cpus)]
        # Forward engine-provided env INTO the container: `docker run` does not inherit the host
        # client's environment, so without `-e` the LOOPLAB_EVAL_SEED (and any eval env) never
        # reaches the eval — every confirm seed would read the default seed and the variance gate
        # would collapse. Mirrors DockerSandbox.run's `-e` forwarding.
        envs: list[str] = []
        for k, v in (env or {}).items():
            envs += ["-e", f"{k}={v}"]
        base = ["docker", "run", "--rm", "--network", network, *rt,
                "--pids-limit", "1024",       # fork-bomb guard (review C1: no pids limit before)
                *caps, *envs,
                "-v", f"{root.as_posix()}:/work", *extra, "-w", cdir]
        return base + [image] + list(argv)

    wrap._docker = True   # marks a real container wrap -> run_command_eval adds in-container timeout
    wrap._mount_root = str(root)   # host_score guards the held-out labels against the MOUNTED root
    return wrap


def _violations(out, wd, constraints, wrap, since=None) -> list[dict]:
    """Read each constraint (a reader spec + a `max`/`min` bound) and return the ones not
    satisfied (incl. a value that couldn't be read — an unverifiable constraint is a
    violation, never a silent pass). Multi-objective gate (#2/#5): a violating node is still
    measured but excluded from best-selection. `since` applies the same freshness gate as the
    primary read — a stale constraint file reads as unverifiable -> violation (fail-closed)."""
    out_list = []
    for c in (constraints or []):
        val = read_metric(out, wd, c, wrap=wrap, since=since)
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
    # Bound both deadlines up front (finite, positive, capped): the docker `_bound` prefix does
    # int(secs), which raises on NaN/inf, and a non-finite deadline never fires (arch-review §4 P1-5).
    timeout = finite_timeout(timeout, 600.0)
    setup_timeout = finite_timeout(setup_timeout, 600.0)
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
    # Freshness gate (§6.3): the eval's OWN work starts now (after setup, which installs deps, not
    # results). Every FILE-based metric/constraint reader below must find a source file written at/after
    # this instant — a stale artifact left by a prior attempt in a reused workdir is rejected, so a
    # command that produced no new output can't promote an old metric. Captured before the child so its
    # writes are strictly newer. stdout readers are inherently this-run and unaffected.
    _eval_started = time.time()
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
        rc, out, err, to = 0, "", "", False      # bound even if every stage is reused/empty (defensive)
        for _i, _stg in enumerate(stages):
            _sname = str(_stg.get("name") or f"stage{_i}")
            _scmd = list(_stg.get("command") or [])
            if _i < _run_from:
                # Reused: an earlier repair attempt already ran this stage and its on-disk artifacts
                # (e.g. the train checkpoint) are kept, so it does NOT re-run. Still emit a zero-work
                # marker span so the trace SHOWS the stage on this re-eval (labeled "reused") instead of
                # the band silently vanishing — otherwise the user sees no Train span after a repair.
                with _sp(_sname, kind="operation", stage=_sname, reused=True):
                    pass
                stage_results.append({"name": _sname, "status": "reused", "exit_code": 0, "seconds": 0.0})
                continue
            _sto = finite_timeout(_stg.get("timeout", timeout), timeout)
            if not _scmd:
                continue
            _t0 = time.monotonic()
            with _sp(_sname, kind="operation", sandboxed=bool(wrap), stage=_sname) as _sh:
                # Live-band anchor: a training subprocess emits NO child LLM/tool spans, and this stage's
                # operation span is written to spans.jsonl only on CLOSE (tracing.Tracer.span), so without
                # a live child the trace view shows nothing for the whole ~hour of training and the
                # "Train"/"Evaluate" block appears only at the end. A zero-work child span carries the
                # phase stamp (see tracing._phase_ctx), which the live view bands under this stage the
                # instant it opens — the intended live mechanism, just given something to anchor on.
                with _sp("stage_started", kind="tool", stage=_sname):
                    pass
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
            # Live-band anchor (see the multi-stage branch): flush a child the instant the single eval
            # command starts, so the "Evaluate" block shows live instead of only when the command ends.
            with _sp("stage_started", kind="tool", phase="evaluate"):
                pass
            rc, out, err, to = run_argv(_w(_bound(command, timeout), str(wd)), wd,
                                         timeout + grace, env, max_output_bytes, cancel,
                                         log_path=_log("eval.log"))
            to = to or (is_docker and docker_timed_out(rc))   # 124 (SIGTERM) or 137 (SIGKILL escalation)
            if _h is not None:
                _h.set_many(exit_code=rc, timed_out=to)
    with _sp("read_metric", kind=metric.get("kind", "stdout_json")):
        m = read_metric(out, str(wd), metric, wrap=wrap, since=_eval_started) if not to else None
    drift = None
    if enforce_drift and cross_check and m is not None:
        validate_cross_check(cross_check)
        cross = read_metric(out, str(wd), cross_check, wrap=wrap, since=_eval_started)
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
                 if (v := read_metric(out, str(wd), spec, wrap=wrap, since=_eval_started)) is not None}
                if (metrics and not to) else {})   # a MISSED reader (None) must not erase a
    #                                                successfully auto-captured value of the same name
    # Auto-capture: every other numeric key on the metric's own JSON line is also reported (no config
    # needed), so an experiment that prints {"metric": x, "recall@10": y, ...} surfaces them all. A
    # declared spec wins over the auto-captured value of the same name.
    auto = (json_line_extras(out, metric.get("key", "metric"))
            if (not to and metric.get("kind", "stdout_json") == "stdout_json") else {})
    extra = ({**auto, **declared} or None)
    viol = (_violations(out, str(wd), constraints, wrap, since=_eval_started)
            if (constraints and not to and m is not None) else None)
    # Intra-node sweep: a RepoTask command may emit the same `{"trials": [...]}` stdout line; carry
    # it so the engine can collapse it to the node's best metric (no eval_spec change required).
    trials = json_line_trials(out) if not to else None
    return RunResult(exit_code=rc, stdout=out, stderr=err, metric=m, timed_out=to, drift=drift,
                     extra_metrics=extra, violations=(viol or None), trials=trials,
                     stages=stage_results)


