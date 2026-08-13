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
import logging
import math
import os
import posixpath
import re
import sys
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# The DECLARED ENVIRONMENT rule lives in `core/envsafe.py` — `core/config.py::Settings.eval_env`
# is the third declarer of the same contract and `core` may not import `runtime`, so the rule sits
# where all three can reach it. Re-exported here because THIS is where the stage contract is read.
from looplab.core.envsafe import (ENGINE_OWNED_ENV, MAX_ENV_VALUE_CHARS,  # noqa: F401 (re-export)
                                  MAX_STAGE_ENV_VARS, merge_env, validate_env_map)
from looplab.runtime.sandbox import (RunResult, _to_float, docker_gpu_argv,
                                     docker_gpu_env, docker_run_argv, docker_timed_out,
                                     finite_timeout, json_line_extras,
                                     json_line_metric, json_line_trials, require_docker_cli,
                                     run_argv)

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
MAX_STAGE_COUNT = 16

# --- The per-stage SUCCESS CONTRACT (`expect`) --------------------------------------------------
# WHAT A STAGE'S SUCCESS MEANS, declared by whoever declared the stage. Exit 0 is not an answer:
# measured live on `runs/rubert-dr-0807`, a model-invented `mine` stage exited 0 having written
# `hard_negs.pkl` for 9,364 of 764,676 unique queries — 1.2% — and `train` was then handed
# `--n_hard_negatives 4` as if every query had them. Nothing objected, because the only two things
# LoopLab checked were the exit code and an LLM sanity read of the log tail whose prompt CORRECTLY
# forbids quality judgements (ranking is the search's job, not the checker's).
#
# The two halves answer the two different questions failure raises, and neither subsumes the other —
# which is exactly why the BACKLOG's existence-only sketch would not have caught this one:
#   • `files` — TECHNICAL, deterministic, no LLM: the artifacts this stage claims to produce must
#     exist inside the workdir, be non-empty, and be FRESH (written by THIS stage, not left over by
#     a previous attempt or a foreign experiment). `hard_negs.pkl` passes this. It is still worth
#     having: it is the half that catches the stale/foreign artifact, and it costs a stat().
#   • `assert` — ONE LINE stating the condition the stage's success MEANS, in the declarer's own
#     words ("hard negatives for at least 90% of the training queries"). This is the half that
#     catches 1.2%, and it is NOT a quality judgement: the search ranks metrics, this asks whether
#     the stage did the job it itself declared. A checker with no contract cannot tell those apart,
#     which is why it was right to forbid it the attempt; a checker holding the declarer's own
#     sentence can.
#
# IT LIVES IN THE STAGE MANIFEST, NOT IN THE SCRIPT, and that is load-bearing. The scorer script is
# Developer-authored on most repo tasks and operator-PROTECTED on some (the write gate refuses the
# agent any edit to it), so a contract that could only be expressed as an assert INSIDE the script
# would be undeclarable exactly when the operator owns the file. A manifest field is declarable in
# both modes: the operator writes it on `cmd.stages`, the Developer writes it on
# `looplab_stages.json`. The in-script assert is the recommended belt (see the repo-Developer
# prompt's STAGE CHECKS block); this is the braces, and it is the half the ENGINE can enforce.
#
# THE OTHER HALF, added 2026-08-13: `needs` — what a stage READS.
#
# `expect` describes only what a stage WRITES, so nothing in the manifest ever stated what a stage
# consumes, and every failure of that kind arrived as a crash deep inside somebody's loader — or, far
# worse, did not arrive at all. Both of the run-killing path defects on this box are of that shape:
# `rubertlite-dr-unified-v5` node 0 wrote its checkpoint exactly where it declared and the scorer read
# a different directory; `v6` node 4 trained a 0.726 model and then SCORED A HUMAN'S JULY CHECKPOINT
# because an absolute path in an editable config named it, with the artifact contract PASSING (the
# stage did write everything it promised) and the run recording 0.225 as a real result.
#
# `needs` is deliberately NOT inside `expect`: `expect` is a claim about this stage's own output,
# checked AFTER it runs, and a precondition is a different fact checked BEFORE — with a different
# remedy. A missing output means the code or the declaration is wrong; a missing input means this
# stage should not have started, and usually that an EARLIER stage put the file somewhere else. That
# distinction is what the check buys: when a declared input is absent, the engine names the earlier
# stage whose `expect.files` promised it, so "train wrote the checkpoint one directory over" is
# reported as that sentence instead of as a traceback in a loader.
#
# It is a stat() before the command, so it costs nothing and it fails BEFORE the GPU-hours, not after.
MAX_STAGE_EXPECT_FILES = 16
MAX_STAGE_NEEDS_FILES = 16
MAX_STAGE_ASSERT_CHARS = 300
# Closed key set. An unknown key is REFUSED, not ignored: `expect`'s whole value is that a declared
# condition is actually checked, so a silently-dropped `"assets"` typo would leave a stage
# advertising a contract nothing enforces — which is the failure this field exists to end.
STAGE_EXPECT_KEYS = ("files", "assert")


def _validate_rel_paths(nm: str, field: str, values) -> tuple[Optional[list], Optional[str]]:
    """The path-SHAPE rule both declaration lists go through: workdir-relative, no traversal, no NUL,
    de-duplicated, order preserved, bounded.

    One function rather than two copies because the two lists are read by opposite halves of the same
    contract (`expect.files` after the stage, `needs` before it) and a shape one side accepts and the
    other refuses would be a manifest that validates and then behaves differently depending on which
    check reaches it first. Containment is re-decided at check time against the REAL workdir by
    `_confined` — a symlink the candidate plants at eval time is invisible from a manifest."""
    cap = MAX_STAGE_NEEDS_FILES if field == "needs" else MAX_STAGE_EXPECT_FILES
    if not isinstance(values, list) or not all(isinstance(f, str) for f in values):
        return None, f"stage {nm!r} `{field}` must be a list of workdir-relative path strings."
    if len(values) > cap:
        return None, f"stage {nm!r} `{field}` may name at most {cap} paths."
    out: list = []
    for f in values:
        rel = f.strip()
        while rel.startswith("./"):
            rel = rel[2:]
        if not rel or "\x00" in rel:
            return None, f"stage {nm!r} `{field}` contains an empty path."
        if os.path.isabs(rel) or (len(rel) > 1 and rel[1] == ":"):
            return None, (f"stage {nm!r} `{field}` entry {f!r} must be RELATIVE to the eval "
                          "workdir — an absolute path names something outside this node.")
        if ".." in rel.replace("\\", "/").split("/"):
            return None, (f"stage {nm!r} `{field}` entry {f!r} escapes the eval workdir.")
        if rel not in out:
            out.append(rel)
    return out, None


def _validate_expect(nm: str, expect) -> tuple[Optional[dict], Optional[str]]:
    """Validate one stage's `expect` block into its canonical form: (clean, None) or (None, reason).

    Split out of `validate_stages` rather than inlined because it is the half a DECLARER reads its
    error message from — the `declare_stages` tool bounces these strings straight back to the model —
    so each refusal has to name the fix, and that is a lot of text to bury inside a loop body.

    Paths are checked for SHAPE here (relative, no traversal, no NUL); actual containment is
    re-decided at verification time against the real workdir by `_confined`, because a symlink the
    candidate creates at eval time cannot be seen from a manifest.
    """
    if not isinstance(expect, dict):
        return None, (f"stage {nm!r} `expect` must be an object like "
                      "{\"files\":[\"ckpt/model.pt\"],\"assert\":\"what success means\"}.")
    unknown = [k for k in expect if k not in STAGE_EXPECT_KEYS]
    if unknown:
        return None, (f"stage {nm!r} `expect` has unknown key(s) {sorted(unknown)!r}; only "
                      f"{list(STAGE_EXPECT_KEYS)!r} are checked. A key nothing reads would advertise "
                      "a contract that is never enforced.")
    clean: dict = {}
    files = expect.get("files")
    if files is not None:
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            return None, f"stage {nm!r} `expect.files` must be a list of workdir-relative path strings."
        if len(files) > MAX_STAGE_EXPECT_FILES:
            return None, f"stage {nm!r} `expect.files` may name at most {MAX_STAGE_EXPECT_FILES} paths."
        out, err = _validate_rel_paths(nm, "expect.files", files)
        if err is not None:
            return None, err
        if out:
            clean["files"] = out
    a = expect.get("assert")
    if a is not None:
        if not isinstance(a, str):
            return None, f"stage {nm!r} `expect.assert` must be a one-line string."
        a = " ".join(a.split())[:MAX_STAGE_ASSERT_CHARS]
        if a:
            clean["assert"] = a
    if not clean:
        # An `expect` that declares nothing is worse than no `expect`: it reads, to anyone auditing
        # the manifest, like the stage carries a contract. Refuse it at authoring time.
        return None, (f"stage {nm!r} `expect` declares nothing — give it `files` (the artifacts this "
                      "stage produces) and/or `assert` (one line stating what its success MEANS), or "
                      "drop the key.")
    return clean, None


# STALL watchdog window: a stage (train/eval) that emits NOTHING for this long while still ALIVE is
# treated as hung (a distributed/DataParallel finalize deadlock, a wedged CUDA op, a lock never
# released) and tree-killed early — instead of sitting until the full, often multi-hour, stage
# deadline. Bounded generously so a legitimately slow-but-quiet phase (hard-negative mining, a large
# FAISS build, a checkpoint save) is not falsely killed; a real deadlock still dies in ~minutes.
# The DEFAULT stall cap (30 min). It is now OPERATOR-OVERRIDABLE: the engine threads
# `Settings.eval_stall_timeout_s` in as `run_command_eval(stall_cap=...)`, so a legitimately quiet
# stage (non-Python train wrapper with block-buffered stdout, a repo script logging only to its own
# file — PYTHONUNBUFFERED helps Python children only) can raise the cap or set it to 0 to disable the
# watchdog. Surfaced to the Developer (agents/roles.py) so its code emits periodic progress to stay alive.
_MAX_STALL_S = 1800.0


def _stall_window(timeout: float, cap: Optional[float] = None) -> Optional[float]:
    """Silence-before-kill for a stage, capped at `cap` (default `_MAX_STALL_S`) and never longer than
    the stage's own deadline (for a short scorer the deadline fires first, so the stall check is a
    harmless no-op). A `cap` of 0/None DISABLES the watchdog (only the hard deadline bounds the stage)."""
    if not timeout or timeout <= 0:
        return None
    if cap is None:
        cap = _MAX_STALL_S
    if cap <= 0:                    # operator opt-out: no silence-based kill, only the hard deadline
        return None
    return min(float(cap), float(timeout))


def _salvageable_stall(signals: dict) -> bool:
    """The authenticated STALL verdict, minus the case DIVERGED already condemned.

    `RunResult.stalled` exists to let evaluate.py's gate (`metric is not None and not timed_out and
    (exit == 0 or stalled)`) rescue a train+eval that printed its metric and then hung on teardown.
    But BOTH watchdogs can fire on one stage: a run that logs non-finite records and then goes silent
    is stall-killed first, and the drain's final decoder flush confirms the divergence afterwards.
    Reporting that as a plain stall handed the salvage gate a self-reported metric from a training the
    DIVERGE watchdog deliberately failed closed on. Divergence is the stronger, fail-closed verdict,
    so it wins — `RunResult` carries no `diverged` field, which is why the pair is resolved here."""
    return bool(signals.get("stalled")) and not signals.get("diverged")


def safe_stage_name(name: str) -> bool:
    """True when `name` is a filesystem-safe stage slug (see `_STAGE_NAME_RE`): rejects separators,
    drives, control/NUL bytes, and `.`/`..` dot segments that would escape the log directory."""
    return bool(name) and ".." not in name and bool(_STAGE_NAME_RE.match(name))


def _dig(obj, key: str):
    """Fetch a possibly-dotted key from nested dicts: 'metrics.val_acc'.

    `key` comes from the eval spec, which Genesis authors from LLM JSON and `_valid_metric_kind`
    validates only for `kind` — so a natural model error like `"key": ["metrics", "val_acc"]` (a list
    instead of the dotted string) used to raise AttributeError from `key.split(".")`. That escaped
    `read_metric` into the eval worker, whose only handler is `except GpuPinUnenforceable`, tearing the
    run down with NO terminal event for the node and re-crashing on every resume. Abstain instead —
    the node fails with no_metric, exactly as the sibling `group` coercion does.
    """
    if not isinstance(key, str):
        return None
    cur = obj
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _regex_metric(text: str, pattern: str, group=1) -> Optional[float]:
    # A bad operator-supplied pattern (re.error), an out-of-range group (IndexError), OR a non-integer
    # `group` (int() raising TypeError/ValueError on e.g. "x"/"1.5"/None) must all read as "no metric",
    # not crash the eval — every other malformed-spec branch in read_metric fails the NODE, not the run.
    # Coerce inside the try so a hostile/typo'd `group` can't propagate out of read_metric and tear the
    # eval task down with no terminal event (which then re-crashes deterministically on every resume).
    try:
        group = int(group)
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
    except (re.error, IndexError, TypeError, ValueError):
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


# Ceiling on a HOST read of a CANDIDATE-written metric/prediction file. The stdout path is already
# bounded (`max_output_bytes`) because unbounded host-RAM buffering is a DoS (sandbox.run_argv); the
# file readers were NOT. Under the untrusted tier the candidate writes into its bind-mounted workdir,
# so an adversarial multi-GB predictions.json / metrics file, slurped whole via read_text on the HOST,
# OOM-kills the engine — not just the one node. A real metric line is a few bytes and even a large
# held-out prediction set is tens of MB, so reject a file above this ceiling as pathological (fail the
# NODE, not the run). Labels are host-held (trusted) and read separately, so this bounds only the
# candidate's own output.
_MAX_METRIC_FILE_BYTES = 256 * 1024 * 1024   # 256 MiB


def _read_metric_file(p: Path) -> Optional[str]:
    """Read a candidate-written metric/prediction file, size-bounded so a huge adversarial file cannot
    OOM the host. Returns None if the file is missing, unreadable, or larger than _MAX_METRIC_FILE_BYTES.
    The eval command has already exited before this runs, so stat().st_size is a stable, race-free gate
    (no concurrent writer). utf-8-sig strips a UTF-8 BOM (common on Windows-written files) that would
    otherwise make json.loads fail / a first-line regex miss."""
    try:
        if p.stat().st_size > _MAX_METRIC_FILE_BYTES:
            return None
        return p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _confined(workdir, rel) -> Optional[Path]:
    """``workdir / rel``, resolved — or None when it does not land INSIDE ``workdir``.

    THE containment guard for every metric-source path, written once (doc 25 RA-05) instead of the
    three hand-copies it replaces. `.resolve()` collapses `..` and symlinks, so an absolute `path` in
    the spec, a `../` traversal, and a symlink pointing out of the sandbox all fail here alike.

    What it is guarding is not uniform, which is why a fourth reader forgetting it is the plausible
    next bug rather than a cosmetic one: for the file readers and for `host_score`'s predictions it
    stops an answer-key (or planted-result) read; for the `adapter` reader it stops an arbitrary
    HOST .py being handed to `runpy`, i.e. code execution.

    A failure out of `.resolve()` is a REFUSAL, not a crash: a malformed spec must fail the NODE,
    like every other malformed-spec branch here, not take down the run.

    `RuntimeError` is in that list deliberately, and it is a FIX, not a widening for tidiness. All
    three hand-copies caught only `(OSError, ValueError)`, but `Path.resolve()` raises
    `RuntimeError("Symlink loop from ...")` — and the candidate can CREATE that loop inside its own
    workdir at eval time (`ln -s b a; ln -s a b`, then a `file_json` spec naming `a`). Pre-extraction
    that escaped `read_metric` and took down the run instead of failing the node.
    """
    try:
        path = (Path(workdir) / rel).resolve()
        return path if _is_within(path, Path(workdir).resolve()) else None
    except (OSError, ValueError, RuntimeError):
        return None


def _read_stdout_json(stdout, workdir, spec, wrap, since) -> Optional[float]:
    return json_line_metric(stdout, spec.get("key", "metric"))


def _read_stdout_regex(stdout, workdir, spec, wrap, since) -> Optional[float]:
    pat = spec.get("pattern") or spec.get("key")   # key = tolerant fallback (composable authoring)
    return _regex_metric(stdout, pat, spec.get("group", 1)) if pat else None


def _read_file(stdout, workdir, spec, wrap, since) -> Optional[float]:
    """`file_json` / `file_regex`: parse a file the candidate wrote into its own workdir."""
    fp = spec.get("path")
    if not fp:
        return None                                 # malformed spec must fail the NODE, not crash the run
    p = _confined(workdir, fp)
    if p is None or not p.is_file():
        return None
    if not _file_is_fresh(p, since):
        return None                                 # stale prior-attempt file in a reused workdir
    text = _read_metric_file(p)                      # size-bounded: a huge candidate file can't OOM the host
    if text is None:
        return None
    if spec.get("kind") == "file_regex":
        pat = spec.get("pattern") or spec.get("key")
        return _regex_metric(text, pat, spec.get("group", 1)) if pat else None
    try:
        return _to_float(_dig(json.loads(text), spec.get("key", "metric")))
    except json.JSONDecodeError:
        return None


def _read_host_score(stdout, workdir, spec, wrap, since) -> Optional[float]:
    # B1 host-side scoring (trust): the candidate WRITES predictions into its workdir; the HOST
    # scores them against held-out labels it holds at a path OUTSIDE the candidate's workspace
    # (never mounted under the untrusted tier, never writable by the candidate). The metric is
    # computed here, on the host — the candidate cannot self-report or see the labels. This turns
    # `stdout_json` self-reporting into an enforced guarantee for untrusted real tasks.
    #
    # Contain the CANDIDATE-controlled predictions path INSIDE the workdir. A `../preds.json` (or an
    # absolute path, or a symlink out) would read a stale/planted file outside the attempt workspace
    # and could return a perfect score (arch-review §3 P0-7). Labels are guarded to be OUTSIDE the
    # workspace below; predictions must be INSIDE it.
    preds_path = _confined(workdir, spec.get("predictions", "predictions.json"))
    if preds_path is None:
        return None
    # A host_score spec with no `labels` path is malformed — `_valid_metric_kind` accepts it (it
    # only checks `kind`), so guard here and FAIL THE NODE (return None) rather than raising a
    # KeyError that has no terminal event and crashes the whole run (like every other malformed-spec
    # branch in this function).
    _lbl = spec.get("labels")
    if not _lbl:
        return None
    labels_path = Path(_lbl).resolve()   # operator-declared host path (trusted)
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
        # FAIL THE NODE, NOT THE RUN. "Fail loud on a misconfiguration" was loud in the wrong
        # direction: this raise escapes `read_metric`, `run_command_eval` AND `_run_eval` — measured
        # — and `engine/evaluate.py`'s only handler around the eval worker is
        # `except GpuPinUnenforceable`, while the orchestrator's dispatchers wrap `_evaluate` in
        # try/FINALLY, not except. So an operator whose held-out labels happen to sit under the run
        # root got NO TERMINAL EVENT and a run that re-died on every resume. That is exactly the
        # failure class `READER_PATH_KEYS` exists to prevent, one reader over. Reachable from three
        # call sites (the primary read, `_violations`, and the drift `cross_check`), and every other
        # malformed-spec branch in this module returns None — this was the one that did not.
        #
        # The loudness belongs at SUBMIT time, where the operator can still fix it, not at score time
        # inside a worker thread. Here it scores nothing and says so — and the submit-time refusal
        # that makes "scores nothing" something an operator hears about is `host_score_labels_error`,
        # asked by `adapters/repo_task.py::EvalSpec` before the run starts. This branch stays because
        # that refusal cannot reach every spec that gets here: `_grandfathered` reloads a run's
        # recorded `task.snapshot.json` without re-validating it (invariant #6).
        logging.getLogger(__name__).error(
            "host_score labels path %s is inside the candidate workspace %s — it would be "
            "mounted/writable by the candidate. Place the held-out labels outside the eval "
            "workspace. This eval scores nothing.", labels_path, guard_root)
        return None
    if not preds_path.is_file() or not labels_path.is_file():
        return None
    if not _file_is_fresh(preds_path, since):
        # The candidate's predictions must be written by THIS eval — a stale predictions.json from a
        # prior attempt in a reused workdir could otherwise score as a perfect result (false promo).
        return None
    preds_text = _read_metric_file(preds_path)      # candidate-controlled: size-bounded against host OOM
    if preds_text is None:
        return None
    try:
        preds = json.loads(preds_text)
        labels = json.loads(labels_path.read_text(encoding="utf-8-sig", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None
    return _to_float(host_score(spec.get("scorer", "rmse"), preds, labels, key=spec.get("key")))


def _read_adapter(stdout, workdir, spec, wrap, since) -> Optional[float]:
    # A (human-ratified, frozen) agent-written module exposing read_metric(workdir)->
    # float, for an arbitrary tracker (TensorBoard/ClearML/custom). Run as a SUBPROCESS
    # in the workdir (not in-process) so it inherits the same timeout/tree-kill harness
    # and can't hang or crash the orchestrator; its printed metric is parsed back.
    rel = spec.get("path", "LOOPLAB_adapter.py")
    # Contain the adapter module INSIDE the workdir before EXECing it: an absolute or `../` path
    # (or a symlink out) would runpy an arbitrary host .py — a code-exec escape the file readers
    # already guard but this branch did not (arch-review §3 P0-7).
    ap = _confined(workdir, rel)
    if ap is None or not ap.is_file():
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


# THE closed set of metric readers, and the only enumeration of it. The kinds used to be spelled out
# three times — this if-chain, `repo_task.EvalSpec._valid_metric_kind`'s local set, and a
# `METRIC_READERS` constant whose docstring claimed to be shared but had zero consumers (doc 25
# RA-04/RA-05). The validator now reads THIS dict, so a new reader is unconfigurable until it is
# registered here, and registering it is what routes it past `_confined`.
METRIC_READERS = {
    "stdout_json": _read_stdout_json,
    "stdout_regex": _read_stdout_regex,
    "file_json": _read_file,
    "file_regex": _read_file,
    "host_score": _read_host_score,
    "adapter": _read_adapter,
}


# The readers whose spec is DEAD without a `path`, registered beside the reader table that decides
# it: `_read_file` returns None the instant `spec["path"]` is missing, while `_read_adapter` DEFAULTS
# its path and the stdout readers have none — so "needs a path" is a per-reader fact, not a property
# of file-ness, and it belongs next to `METRIC_READERS` for the same reason the kind set does (doc 25
# RA-04/RA-05). Without a submit-time check the omission is invisible: `key` is the key INSIDE the
# file (never the file name), so a `{"kind":"file_json","key":"metric"}` spec validates on its kind,
# the run starts, and EVERY node fails `no_metric` with nothing naming the cause.
READERS_REQUIRING_PATH = frozenset({"file_json", "file_regex"})


def spec_kind(spec) -> str:
    """`spec["kind"]` as something safe to look a reader up BY, defaulting like the readers do.

    Every `kind` lookup in this module goes through here, because the raw value is operator- or
    model-authored JSON and `dict.get` is not total over it: `{"kind": ["file_json"]}` raises an
    uncaught `TypeError: unhashable type: 'list'` out of `METRIC_READERS.get(...)`, which escapes
    `read_metric` into the eval worker exactly as a non-string path slot did — no node terminal, and
    a run that re-dies on every resume. It escaped `metric_spec_path_error` too, so the submit-time
    refusal that exists to catch malformed specs crashed on this one instead of naming it.

    An unhashable or non-string kind resolves to a sentinel that is in no reader table, so it takes
    the SAME route an unknown kind already takes — `read_metric` returns None and the node fails
    `no_metric`, `_valid_metric_kind` refuses it at submit. Deliberately not `"stdout_json"`: that is
    the default for an ABSENT kind, and silently scoring a malformed spec with the default reader
    would turn a typo into a plausible-looking metric.
    """
    kind = spec.get("kind", "stdout_json") if isinstance(spec, dict) else "stdout_json"
    return kind if isinstance(kind, str) else "\x00unhashable-or-non-string-kind"


# The spec keys each reader turns into a FILESYSTEM PATH, and the only enumeration of them. This is
# NOT `READERS_REQUIRING_PATH` and must not be folded into it: "needs a path" and "names a path" are
# different facts about a reader. `_read_adapter` DEFAULTS its path (so it needs none) and still
# builds one out of `spec["path"]` whenever the operator supplies one; `_read_host_score` names TWO
# files, under other key names entirely, and one of them (`labels`) is the HOST-held answer key that
# goes to `Path(...)` DIRECTLY rather than through `_confined` — so it is not even a `_confined`
# call site, and a table keyed on "does this reader call `_confined`" would miss it.
# It sits beside `METRIC_READERS` for the same reason the kind set does: a new reader whose path slot
# is missing here is one `Path(workdir) / 123` away from taking the RUN down (see
# `metric_spec_path_error` and `read_metric`, which are its two consumers).
READER_PATH_KEYS = {
    "file_json": ("path",),
    "file_regex": ("path",),
    "adapter": ("path",),
    "host_score": ("predictions", "labels"),
}


# The corrected spec shown alongside a refusal, per reader. `file_json`/`file_regex` keep verbatim the
# example the missing-`path` message has always shown (tests pin the literal); `adapter` and
# `host_score` are reachable only through the non-string check and need their own, because telling a
# `host_score` author to put the file in `path` sends them to a spec that reads nothing.
_PATH_EXAMPLES = {
    "file_json": '{"kind": "file_json", "path": "metrics.json", "key": "metric"}',
    "file_regex": '{"kind": "file_regex", "path": "metrics.json", "key": "metric"}',
    "adapter": '{"kind": "adapter", "path": "LOOPLAB_adapter.py"}',
    "host_score": ('{"kind": "host_score", "predictions": "predictions.json", '
                   '"labels": "/held-out/labels.json", "scorer": "rmse"}'),
}


def _nonstring_path_slot(spec) -> Optional[tuple]:
    """The first `(key, value)` of `spec` that names a metric-source FILE but is not a string.

    ONE statement of the rule for two consumers whose jobs differ: `metric_spec_path_error` turns it
    into a submit-time refusal naming the field, and `read_metric` into a runtime ABSTENTION (the node
    fails `no_metric`, like every other malformed-spec branch here) for the documents that refusal can
    never reach — `adapters/repo_task.py::_grandfathered` reloads a run's recorded `task.snapshot.json`
    WITHOUT re-validating it, which is exactly the spec that has already proved it crashes.

    An ABSENT slot is not this function's business — whether a reader is dead without its path is the
    per-reader fact `READERS_REQUIRING_PATH` holds. `str` is the only accepted spelling on purpose:
    these specs are authored as JSON, where a path is a string, and every other type either raises
    TypeError out of the path constructor (int/float/list/dict, and `bytes` too) or would need
    confinement re-derived for a second representation.
    """
    for slot in READER_PATH_KEYS.get(spec_kind(spec), ()):
        value = spec.get(slot)
        if value is not None and not isinstance(value, str):
            return slot, value
    return None


# The slot-agnostic tail of the refusal. WHAT a pathless reader costs depends on which slot it is
# in, and the four costs are genuinely different — measured: a pathless `metrics` entry is dropped
# from the node's report, a pathless `constraints` entry reads as unverifiable (which `_violations`
# counts as a violation, so EVERY node is excluded from best-selection), a pathless `cross_check`
# makes `_drift` fail closed (so every node's metric is discarded), and only the primary `metric`
# fails `no_metric`. A message naming the WRONG symptom is worse than one naming none — it sends the
# operator hunting the wrong failure — so the consequence sentence is the CALLER's
# (`adapters/repo_task.py::_PATHLESS_COST` holds the four), and only this always-true fallback,
# "which readers need a path", and the corrected example live here.
_PATHLESS_COST = "Without `path` this reader can never return a value."


def metric_spec_path_error(spec, *, consequence: Optional[str] = None) -> Optional[str]:
    """Why `spec` can never read a metric for want of a usable `path` — or None when it can.

    A PRESENT but NON-STRING path slot is rejected too, and is the worse of the two failures: it takes
    down the RUN rather than failing the node. `_confined` catches only
    (OSError, ValueError, RuntimeError), so `Path(workdir) / 123` raises an uncaught TypeError out of
    `read_metric`, as does `_read_host_score`'s `Path(<labels>).resolve()`; the eval worker's only
    handler is `except GpuPinUnenforceable` (`engine/evaluate.py`), so the node gets NO terminal event
    and the run re-dies on every resume.

    That check is asked of EVERY reader that builds a path out of the spec (`READER_PATH_KEYS`), not
    only the two that REQUIRE one. It used to sit INSIDE the `kind not in READERS_REQUIRING_PATH`
    early exit, which is why it protected `file_json`/`file_regex` alone and left `adapter`'s `path`
    and `host_score`'s `predictions`/`labels` behind it — all three measured to raise TypeError out of
    a real `read_metric` call. Do NOT fix that by adding those kinds to `READERS_REQUIRING_PATH`: a
    MISSING path is a per-reader fact and stays exactly as it was (`_read_adapter` defaults its path,
    the stdout readers have none), while a non-string one is universal.

    `consequence`: the caller's MEASURED failure mode for its own reader slot (see `_PATHLESS_COST`).
    """
    if not isinstance(spec, dict):
        return None
    kind = spec_kind(spec)
    bad = _nonstring_path_slot(spec)
    if bad is not None:
        slot, value = bad
        return (f"metric reader {kind!r} needs `{slot}` to be a STRING file path, got "
                f"{type(value).__name__}: {str(value)[:60]!r}. e.g. {_PATH_EXAMPLES[kind]}")
    if kind not in READERS_REQUIRING_PATH:
        return None
    path = spec.get("path")
    if isinstance(path, str) and path.strip():
        return None
    example = _PATH_EXAMPLES[kind]
    return (f"metric reader {kind!r} needs a `path`: the file the eval writes, relative to the node's "
            f"eval workdir. e.g. {example} — `key` is the key INSIDE that file, not the file name. "
            + (consequence or _PATHLESS_COST))


# The submit-time half of `_read_host_score`'s held-out-labels invariant, and the reason it has to
# exist SEPARATELY from the runtime check. The runtime one used to RAISE, which killed the run (no
# terminal event, re-dying on every resume — see `_read_host_score`); it now logs and scores nothing,
# which is the only safe thing to do inside an eval worker. But "this eval scores nothing" is a
# whisper: every node fails `no_metric` and the run finishes with no best node, exactly the shape
# `_PATHLESS_COST` was written for. An operator has to hear about it while they can still move the
# file, so the loudness moved HERE — asked once, before the run starts, by
# `adapters/repo_task.py::EvalSpec`.
_LABELS_REQUIRED = (
    'metric reader \'host_score\' needs `labels`: the HOST-held answer key it scores the '
    'candidate\'s predictions against, e.g. {"kind": "host_score", "predictions": '
    '"predictions.json", "labels": "/held-out/labels.json", "scorer": "rmse"}. Without it the '
    'reader scores nothing and every node fails no_metric.')
_LABELS_RELATIVE = (
    "host_score `labels`={path!r} is a RELATIVE path. It is resolved against whatever working "
    "directory the ENGINE process happens to have — not the eval workdir — so which file is graded "
    "depends on where the run was launched from, and a run started inside the run directory grades "
    "against a file the candidate can write. Give the absolute host path to the held-out labels.")
_LABELS_INSIDE = (
    "host_score `labels`={path!r} resolves INSIDE the candidate workspace {root!r}. Held-out labels "
    "are the one thing the candidate must not be able to read or rewrite — under the untrusted tier "
    "the whole run root is bind-mounted into the container — so this defeats host-side grading "
    "entirely. Place the labels outside the run directory. (At score time the reader refuses this "
    "spec and the node gets NO metric, so the run would produce no best node either.)")


def host_score_labels_error(spec, *, workspace_root=None) -> Optional[str]:
    """Why this `host_score` spec's held-out labels are unusable — or None when they are fine.

    The rule `_read_host_score` enforces at score time, stated once so a submit surface can refuse it
    while the operator can still act. Three failures, and they are not the same failure:

      * ABSENT `labels` — the reader returns None for every node, and nothing anywhere says why.
      * RELATIVE `labels` — `Path(labels).resolve()` binds to the ENGINE process's cwd. Which file is
        graded then depends on the launch directory, and this is also the most common way a labels
        path ends up inside the workspace by accident.
      * INSIDE the workspace — the invariant itself. `workspace_root` is the run directory (the
        docker tier bind-mounts the whole of it, so it is the guard root a real eval uses); omit it
        and only the two spec-local rules are checked, which is what a caller that does not yet know
        where the run will live can honestly say.

    Non-`host_score` specs are None: this is one reader's rule, asked of every reader slot.
    """
    if not isinstance(spec, dict) or spec_kind(spec) != "host_score":
        return None
    labels = spec.get("labels")
    if labels is None or (isinstance(labels, str) and not labels.strip()):
        return _LABELS_REQUIRED
    if not isinstance(labels, str):
        return None                       # `metric_spec_path_error` owns the non-string slot message
    if not os.path.isabs(labels):
        return _LABELS_RELATIVE.format(path=labels)
    if workspace_root is None:
        return None
    try:
        inside = _is_within(Path(labels).resolve(), Path(workspace_root).resolve())
    except (OSError, ValueError, RuntimeError):
        return None                       # an unresolvable path is not evidence of a violation
    return _LABELS_INSIDE.format(path=labels, root=str(workspace_root)) if inside else None


def read_metric(stdout: str, workdir: str, spec: dict, wrap=None,
                since: Optional[float] = None) -> Optional[float]:
    """Read the metric for one eval according to `spec` (an eval_spec['metric']). Built-in
    readers parse host files/stdout in-process (data, never code). The `adapter` reader EXECS
    agent-authored code, so under the untrusted tier it must run in the same sandbox as the
    eval — pass `wrap` (from make_docker_wrap) to run it inside the container.

    `since` (eval start time, from run_command_eval): FILE-based readers reject a metric-source file
    older than it — a stale artifact left in a reused workdir must not read as this eval's result.

    An unknown `kind` returns None (the node fails with no_metric) exactly as the old if-chain's
    fall-through did; `_valid_metric_kind` is what turns it into a submit-time error instead."""
    # A PRESENT but non-string path slot is a REFUSAL here, not a crash — the same rule
    # `metric_spec_path_error` refuses at SUBMIT, asked again at RUNTIME because the submit-time
    # refusal cannot reach every spec that gets here: `adapters/repo_task.py::_grandfathered` reloads
    # a run's recorded `task.snapshot.json` without re-validating it (CLAUDE.md invariant #6), so a
    # document authored before the refusal existed still runs. Without this the malformed spec raises
    # TypeError out of `_confined`/`Path(...)` (neither of which catches it) into the eval worker,
    # whose only handler is `except GpuPinUnenforceable` — the node gets no terminal event and the RUN
    # dies, then re-dies on every resume. Failing the node is what every other malformed-spec branch
    # in this module does.
    if _nonstring_path_slot(spec) is not None:
        return None
    reader = METRIC_READERS.get(spec_kind(spec))
    return reader(stdout, workdir, spec, wrap, since) if reader is not None else None


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
    except (TypeError, ValueError, OverflowError):
        return str(v)
    if not math.isfinite(f):        # inf/-inf/nan: int(f) below raises OverflowError/ValueError, which
        return repr(f)              # would crash build_command with no terminal event — pass it through
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


# GPU CLI flags reconciled to a SINGLE device when the eval is pinned to one GPU (see
# run_command_eval::_bound). Deliberately a TIGHT allowlist of unambiguous device selectors from the
# DL-training world — capping an unrelated numeric flag would corrupt a command, so anything not
# clearly a GPU/device selector is left untouched. Split by SEMANTICS, because the same number means
# different things: a COUNT flag ("how MANY gpus", `--gpus 2`) caps to "1"; an INDEX flag ("WHICH gpu",
# `--gpu 3`) caps to "0" — under the pin the sole visible ordinal is 0, so a count-style "1" would
# still be an invalid ordinal and crash. `--gpus` is this run's own flag; the rest cover the common
# pytorch-lightning / torchrun / DDP spellings so the fix is universal, not per-repo.
_GPU_COUNT_FLAGS = frozenset({
    "--gpus", "--num_gpus", "--num-gpus", "--n_gpus", "--n-gpus", "--ngpus",
    "--num_devices", "--num-devices", "--devices", "--gpu_count", "--gpu-count",
    "--world_size", "--world-size", "--nproc_per_node", "--nproc-per-node",
})
_GPU_INDEX_FLAGS = frozenset({
    "--gpu", "--gpu_id", "--gpu-id", "--gpu_index", "--gpu-index",
    "--cuda_device", "--cuda-device", "--device_id", "--device-id",
})
_RANGE_RE = re.compile(r"\d+-\d+")   # a device RANGE like "0-3" (distinct from a negative "-1")


def _cap_gpu_value(val: str, *, index: bool = False) -> Optional[str]:
    """Cap ONE gpu-flag value for a command that will run PINNED to a single visible GPU (ordinal 0),
    or return None to leave it unchanged.

    INDEX flag (`--gpu 3` = WHICH gpu): the only visible ordinal is 0, so ANY positive index / list /
    range -> "0"; "0", "-1" ("all"/"auto") and non-numeric are left as-is.
    COUNT flag (`--gpus 2` = HOW MANY): a plain COUNT >1 -> "1"; a device LIST ("0,1") or RANGE ("0-3")
    is really an index spec -> "0"; a single "0"/"1"/non-numeric is left as-is."""
    v = (val or "").strip()
    if not v:
        return None
    is_list = "," in v                                     # "0,1" or single-element "3,"
    is_range = bool(_RANGE_RE.fullmatch(v))                # "0-3" (NOT "-1")
    if index:
        if is_list or is_range:
            return "0"
        if v.lstrip("+").isdigit():                        # a plain non-negative index
            return "0" if int(v) != 0 else None
        return None                                        # "auto" / "-1" / non-numeric
    if is_list or is_range:                                # count flag carrying an index LIST/RANGE
        return "0"
    if v.isdigit():                                        # a plain COUNT (or single index)
        return "1" if int(v) > 1 else None
    return None                                            # "auto" / "-1" / non-numeric


def cap_gpu_flags(argv: list) -> list:
    """Return `argv` with any explicit MULTI-GPU request capped to a SINGLE device, for a command that
    will run PINNED to one GPU (a single-index CUDA_VISIBLE_DEVICES). Handles both `--gpus 2` (flag +
    separate value token) and `--gpus=2` (=-joined) forms for the tight count/index allowlists, and
    caps device COUNTS, INDICES, lists and ranges by their flag's semantics (see `_cap_gpu_value`).
    Pure list[str]->list[str] (mirrors expand_params); unrecognized/ambiguous values pass through
    unchanged so a non-GPU command is never corrupted. Gated by the pin check in run_command_eval::
    _bound — this makes the pin's 'transparent remap to cuda:0' promise TRUE even for a command (the
    editable train stage OR the PROTECTED score command the agent cannot edit) that hardcodes >1 GPU."""
    out: list = []
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if isinstance(a, str) and a.startswith("--") and "=" in a:          # `--flag=value` form
            flag, _, val = a.partition("=")
            if flag in _GPU_COUNT_FLAGS or flag in _GPU_INDEX_FLAGS:
                capped = _cap_gpu_value(val, index=flag in _GPU_INDEX_FLAGS)
                out.append(f"{flag}={capped}" if capped is not None else a)
                i += 1
                continue
        if (isinstance(a, str) and (a in _GPU_COUNT_FLAGS or a in _GPU_INDEX_FLAGS)   # `--flag value`
                and i + 1 < n and isinstance(argv[i + 1], str)):
            capped = _cap_gpu_value(argv[i + 1], index=a in _GPU_INDEX_FLAGS)
            out.append(a)
            out.append(capped if capped is not None else argv[i + 1])
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def validate_stages(stages, *, reserved: tuple = (),
                    allow_env: bool = False) -> tuple[Optional[list], Optional[str]]:
    """Validate a stage list ({name, command:[argv...], timeout?, check?}) into its canonical clean
    form: (clean, None) on success, (None, reason) on the first problem. This is the SINGLE
    definition of "a valid stage", shared by the Developer's `declare_stages` tool (authoring time),
    `EvalSpec.stages` (the operator's cmd pipeline, submit time) and the engine's `_resolve_stages`
    (consume time) — so the two ends of the manifest handshake can't drift: a stage one side accepts
    is never silently dropped or re-interpreted by the other. `reserved` names are refused — the
    engine appends the operator's protected `score` stage to a DEVELOPER manifest, so the tool passes
    ("score",); operator-declared stages reserve nothing (the operator owns scoring).

    `allow_env` is the OTHER half of the same declarer distinction, and it defaults to False for the
    opposite reason `reserved` defaults to empty: a stage `env` is operator-only (see the DECLARED
    ENVIRONMENT block), so the fail-closed direction is to refuse it and let the four operator call
    sites opt in explicitly. A new call site that forgets gets the refusal, not the smuggle."""
    if not isinstance(stages, list) or not stages:
        return None, "`stages` must be a non-empty array of {name, command:[argv...]} objects."
    if len(stages) > MAX_STAGE_COUNT:
        return None, f"`stages` may contain at most {MAX_STAGE_COUNT} entries."
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
        if s.get("expect") is not None:
            # The per-stage SUCCESS CONTRACT (see `_validate_expect` and the block above it). Shared
            # here rather than at each declaring site for the same reason every other stage rule is:
            # `declare_stages` (authoring), `EvalSpec.stages` (operator submit) and `_resolve_stages`
            # (consume) must accept exactly the same thing, or a contract one side records is a
            # contract the other silently never checks.
            exp, err = _validate_expect(nm, s["expect"])
            if err is not None:
                return None, err
            st["expect"] = exp
        if s.get("needs") is not None:
            # The stage's INPUT contract — see the block above `MAX_STAGE_EXPECT_FILES`. Validated
            # here beside `expect` for the same reason: `declare_stages`, `EvalSpec.stages` and
            # `_resolve_stages` must accept exactly the same manifest.
            needs, err = _validate_rel_paths(nm, "needs", s["needs"])
            if err is not None:
                return None, err
            if needs:
                st["needs"] = needs
        if s.get("env") is not None:
            # The stage's DECLARED ENVIRONMENT — see the block above `MAX_STAGE_ENV_VARS`. REFUSED
            # rather than dropped when this declarer may not set it: silently ignoring the key would
            # leave a manifest that reads as if the stage carries an environment nothing applies,
            # which is the same failure `expect`'s closed key set exists to end — and here it would
            # also hide the trust boundary from the agent that just tried to cross it.
            if not allow_env:
                return None, (
                    f"stage {nm!r} may not declare `env`: the environment a stage runs in is the "
                    "OPERATOR's to set, not the Developer's (an agent that can set environment for "
                    "the protected `score` stage has a route around the scorer freeze). Ask the "
                    "operator to put it on the task's `cmd.stages[].env`, `cmd.env`, or the run's "
                    "`eval_env` setting. If your code needs a value, read it with a documented "
                    "default instead.")
            envmap, err = validate_env_map(f"stage {nm!r} `env`", s["env"])
            if err is not None:
                return None, err
            if envmap:
                st["env"] = envmap
        clean.append(st)
    return clean, None


# What `verify_stage_artifacts` reports when a declared artifact is absent/empty/stale. Hoisted as a
# constant family so the three refusals can be pinned and so the repair loop reads ONE vocabulary:
# each names the DECLARATION as the thing that was broken, because that is what tells the Developer
# whether to fix the code or the manifest. `{stage}`/`{path}` are the only placeholders.
EXPECT_MISSING = ("stage {stage!r} exited 0 but did NOT produce its declared artifact {path!r}. The "
                  "stage manifest DECLARES this stage produces it, so either the stage's code never "
                  "wrote it (fix the code) or the declaration names the wrong path (fix "
                  "looplab_stages.json). Do not delete the declaration to make this pass.")
# Appended to EXPECT_MISSING when a file of the SAME NAME was in fact written somewhere else during
# this stage. That is the difference between "your training produced nothing" and "your training
# worked and you named the output wrong" — and the two need opposite repairs.
EXPECT_MISSING_NEARBY = (" A file of that name WAS written by this stage at {found!r} — so the code "
                         "ran and produced output; the declared path is what disagrees with it.")
# How much of the workdir the near-miss scan may look at. It runs ONLY on a failure that has already
# cost the stage's full runtime, so a bounded walk is free by comparison — but a node workdir holds a
# materialized repo plus every checkpoint, on a FUSE mount, so it must still be bounded rather than
# merely "fast enough here".
_NEARBY_MAX_DIRS = 4000
_NEARBY_SKIP = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
                          ".pytest_cache", ".ipynb_checkpoints"})


def _artifacts_written_elsewhere(workdir: str, rel: str, since: Optional[float]) -> list[str]:
    """EVERY place a fresh file with this basename was written during the stage, sorted.

    Answers the question the operator asks first and the message could not: "did it not run, or did
    it write somewhere else?". Measured case that motivated it — `rubertlite-dr-unified-v5` node 0
    trained for 76 minutes on two H200s, exited 0, wrote a complete checkpoint, and died because the
    testbed composes its output directory as `<run_name>_<model>` while the manifest declared
    `<run_name>`. The metric it had already computed was recall@100 = 0.743, and nothing said so.

    FRESH ONLY (`since`), for the same reason the main contract has a staleness rule: a leftover from
    an earlier attempt or a foreign experiment would send the repair at the wrong file. Bounded, and
    best-effort — a scan that fails contributes nothing rather than turning a stage failure into an
    eval crash.

    ALL of them, sorted, rather than the first `os.walk` hit, because the two callers need different
    things from an AMBIGUOUS answer and neither may have "whichever the filesystem yielded first":
    the diagnostic (`verify_stage_artifacts`) shows a human the nearest miss and a human can weigh a
    second one; `engine/metric_salvage._relocated` promotes a NUMBER out of the file it names, and a
    metric chosen by directory-walk order is a metric that can differ between two reads of the same
    workdir. `os.walk` order is filesystem order (`os.scandir`), not lexical — two fresh
    same-basename files are enough to make the choice unstable, so the ordering is imposed here and
    the ambiguity is left visible to the caller.
    """
    name = os.path.basename(str(rel or ""))
    if not name:
        return []
    root = Path(workdir)
    visited = 0
    found: list[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            visited += 1
            if visited > _NEARBY_MAX_DIRS:
                break
            dirnames[:] = [d for d in dirnames if d not in _NEARBY_SKIP and not d.startswith(".")]
            if name not in filenames:
                continue
            candidate = Path(dirpath) / name
            try:
                if candidate.stat().st_size <= 0 or not _file_is_fresh(candidate, since):
                    continue
            except OSError:
                continue
            here = candidate.relative_to(root) if candidate.is_relative_to(root) else candidate
            # Compare the DECLARED path AS A PATH, never as text. `rel` is the operator's spec string
            # — POSIX-spelled by convention, and possibly `./sub/x` — while `here` renders with the
            # HOST separator, so on Windows `sub\x` never equalled `sub/x` and the declared file
            # reported itself as written "elsewhere". That flips the outcome by OS: what
            # `metric_salvage._relocated` would salvage from a single candidate becomes the ambiguous
            # pair it refuses (`len(found) != 1`), on the same run against the same workdir.
            if here != Path(str(rel or "")):
                found.append(str(here))
    except Exception:  # noqa: BLE001 — a diagnostic must never be the thing that fails the eval
        return []
    return sorted(found)


def _artifact_written_elsewhere(workdir: str, rel: str, since: Optional[float]) -> Optional[str]:
    """The nearest miss to SHOW, or None — the diagnostic half of `_artifacts_written_elsewhere`.

    First in sorted order, so the message an operator reads is the same on every run over the same
    workdir. A caller that must not choose between two candidates asks for the list instead.
    """
    found = _artifacts_written_elsewhere(workdir, rel, since)
    return found[0] if found else None
EXPECT_EMPTY = ("stage {stage!r} exited 0 but its declared artifact {path!r} is EMPTY (0 bytes). The "
                "stage reported success while producing nothing — fix the code that writes it.")
EXPECT_STALE = ("stage {stage!r} exited 0 but its declared artifact {path!r} was NOT written by this "
                "run of the stage (its mtime predates the stage start), so what the next stage would "
                "read is a leftover from an earlier attempt or another experiment. The stage must "
                "(re)write it every time it runs — a 'skip if it already exists' shortcut is exactly "
                "what freezes the metric.")
EXPECT_ESCAPES = ("stage {stage!r} declares artifact {path!r}, which does not resolve inside the eval "
                  "workdir (an absolute path, a `..` traversal, or a symlink out of the sandbox). "
                  "Declared artifacts are workdir-relative.")

# THE FLOOR THE CONTRACT WAS HELD TO, recorded on the stage row a contract failure produces.
#
# `verify_stage_artifacts`' freshness half compares `st_mtime` against the stage's OWN wall-clock
# start (`_w0` below), and that number existed nowhere outside `_run_stages`' frame. It has to,
# because the engine now RE-ASKS this same check after a repair corrected the declaration
# (`engine/metric_salvage.py::recheck_floor` — the F1e re-check), and a re-check held to a LATER
# floor would refuse the very artifact the stage wrote, while one held to an EARLIER floor (the
# attempt's start, the eval's) would let a leftover from a previous attempt of this deliberately
# reused workdir satisfy the corrected path. The only floor that means "this run of this stage
# produced it" is the one the original check used, so it is written down rather than re-derived.
#
# Additive and fold-ignored: `replay._on_stage_finished` copies exactly name/status/exit_code/
# seconds out of the row, so this rides on `stage_finished` without changing folded state (the same
# way `concern` already does), and every reader of an older log sees it absent.
EXPECT_SINCE_KEY = "expect_since"

# THE DECLARATION THAT FAILED, recorded beside the floor and for the same reason: the F1e re-check
# needs to know what the contract SAID, not only when it was held.
#
# Without it the re-check can only ask "did the repair write the manifest?" — a question about a
# FILENAME, which is not the same question as "did the repair reach the declaration that failed".
# The gap is a real, first-class shape, not a defensive hypothetical: in operator `cmd.stages` mode
# the failing declaration lives in the task snapshot and `looplab_stages.json` is ignored entirely,
# while the cause-repair prompt tells the Developer to fix `looplab_stages.json` — so the file-set
# gate passes on a repair that corrected NOTHING, and the promotion then turns on whether a second
# `stat()` of the same unchanged path answers differently from the first, across a window containing
# a full Developer LLM round trip. Driven end to end on a real Engine: the node was promoted with the
# still-wrong path recorded as its corrected `expect_files`.
#
# Additive and fold-ignored, exactly like `EXPECT_SINCE_KEY`: `replay._on_stage_finished` copies only
# name/status/exit_code/seconds, and a reader of an older log sees it absent — which the re-check
# treats as "cannot establish", i.e. no promotion.
EXPECT_DECLARED_KEY = "expect_declared"


def verify_stage_artifacts(expect, workdir, since: Optional[float], *, stage: str = "") -> Optional[str]:
    """The TECHNICAL half of the stage success contract: None when every declared artifact is there,
    otherwise a one-line reason naming the first one that is not. Deterministic, no LLM, one stat per
    path.

    THREE conditions, not one, and the reason each is separate is that they fail differently:
      • ABSENT — the stage claimed to produce it and did not. This is the BACKLOG's existence check.
      • EMPTY — a 0-byte file passes existence and carries nothing. Cheap to check, and the shape a
        crashed-then-swallowed writer leaves behind (`open(p,"wb")` then an exception the script ate).
      • STALE — it exists and is non-empty but predates this stage's start, so the next stage would
        read a PREVIOUS attempt's artifact or a foreign experiment's. This is the one that matters
        most on a repaired node, because the workdir deliberately persists across attempts so a
        completed `train` can be reused: everything else in that design is what makes a stale
        artifact plausible here, and `_safe_reuse_start` only bounds the stages the engine SKIPS,
        never the ones it re-runs.

    `since` is the stage's own wall-clock start (`time.time()`), not the eval's — a stage that
    re-runs on attempt 3 must rewrite its artifact on attempt 3. `None` disables the freshness gate
    for callers that have no start time (the check then degrades to existence + non-empty, which is
    still strictly more than exit-code-only).

    A DIRECTORY counts as produced when it exists and is non-empty (a checkpoint dir, a HF
    `save_pretrained` output); its freshness is its own mtime, which POSIX updates when an entry is
    created or removed inside it. Deliberately not a recursive newest-child walk: that is O(tree) in
    the eval path for a checkpoint dir with thousands of shards, and the coarse answer is the
    conservative one here — it can only ever report an untouched directory as stale, never a stale
    one as fresh.
    """
    for rel in ((expect or {}).get("files") or []):
        p = _confined(workdir, rel)
        if p is None:
            return EXPECT_ESCAPES.format(stage=stage, path=rel)
        try:
            st = p.stat()
        except OSError:
            message = EXPECT_MISSING.format(stage=stage, path=rel)
            elsewhere = _artifact_written_elsewhere(workdir, rel, since)
            return message + EXPECT_MISSING_NEARBY.format(found=elsewhere) if elsewhere else message
        if p.is_dir():
            try:
                empty = not any(p.iterdir())
            except OSError:
                empty = True
            if empty:
                return EXPECT_EMPTY.format(stage=stage, path=rel)
        elif st.st_size <= 0:
            return EXPECT_EMPTY.format(stage=stage, path=rel)
        if not _file_is_fresh(p, since):
            return EXPECT_STALE.format(stage=stage, path=rel)
    return None


# What `verify_stage_inputs` reports. Same family shape as the EXPECT_* constants above and for the
# same reason — the repair loop reads ONE vocabulary — but every sentence here names the thing that
# is different about an input: the stage has NOT run, so nothing about its code is implicated yet.
NEEDS_ESCAPES = ("stage {stage!r} declares an input {path!r} that resolves outside the eval workdir. "
                 "Declared inputs are workdir-relative.")
NEEDS_MISSING = ("stage {stage!r} did not start: it DECLARES that it reads {path!r}, and that path "
                 "does not exist in the eval workdir. Either an earlier stage was supposed to write "
                 "it and wrote it somewhere else, or this stage reads a different path than it "
                 "declares. Fix whichever is wrong — do not delete the declaration to make this pass.")
# Appended when an EARLIER stage in this same pipeline declared the missing path as its own output.
# This is the whole point of the input contract: it turns a mismatch between two declarations into
# one sentence naming both stages, instead of a crash inside somebody's loader three stages later.
NEEDS_MISSING_PRODUCER = (" Stage {producer!r} DECLARES this path as one of its own outputs "
                          "(`expect.files`), so the two declarations disagree about where the file "
                          "is, or that stage did not really produce it.")
# Appended when a file of the same NAME exists elsewhere in the workdir — the same near-miss signal
# `EXPECT_MISSING_NEARBY` gives the output side, and the same repair: one of the two paths is wrong.
NEEDS_MISSING_NEARBY = (" A file of that name DOES exist at {found!r}.")
NEEDS_EMPTY = ("stage {stage!r} did not start: its declared input {path!r} exists but is EMPTY. "
               "Whatever was supposed to produce it did not finish — running this stage on a 0-byte "
               "input would either crash inside a loader or silently train on nothing.")


def verify_stage_inputs(needs, workdir, *, stage: str = "", producers: Optional[dict] = None,
                        since: Optional[float] = None) -> Optional[str]:
    """The stage's INPUT contract: None when every declared input is present and non-empty, otherwise
    a one-line reason naming the first one that is not. Checked BEFORE the command runs.

    TWO conditions, not the output side's three, and the missing one is deliberate: an input has NO
    freshness rule. A stage legitimately reads things that predate it by any amount — the seeded
    repo, a mounted dataset, a base checkpoint, a `train` output the engine deliberately REUSED
    across attempts (`_safe_reuse_start`). Applying `expect`'s staleness gate here would refuse the
    reuse the persistent workdir exists for. `since` is accepted so a caller can be explicit about
    that and so the signature matches its output twin, and it is intentionally unused.

    `producers` maps a declared output path to the EARLIER stage that promised it, which is what lets
    the refusal name both sides of a disagreement between two declarations. Absent, the message is
    still correct, just less specific.
    """
    del since  # see the docstring: an input has no freshness rule, and saying so beats omitting it
    for rel in (needs or []):
        p = _confined(workdir, rel)
        if p is None:
            return NEEDS_ESCAPES.format(stage=stage, path=rel)
        try:
            st = p.stat()
        except OSError:
            message = NEEDS_MISSING.format(stage=stage, path=rel)
            producer = (producers or {}).get(rel)
            if producer:
                message += NEEDS_MISSING_PRODUCER.format(producer=producer)
            # `since=None`: the near-miss scan's freshness filter is about "did THIS stage write it",
            # which is meaningless for an input — any copy of that name is worth reporting.
            elsewhere = _artifact_written_elsewhere(workdir, rel, None)
            if elsewhere:
                message += NEEDS_MISSING_NEARBY.format(found=elsewhere)
            return message
        if p.is_dir():
            try:
                if not any(p.iterdir()):
                    return NEEDS_EMPTY.format(stage=stage, path=rel)
            except OSError:
                return NEEDS_EMPTY.format(stage=stage, path=rel)
        elif st.st_size <= 0:
            return NEEDS_EMPTY.format(stage=stage, path=rel)
    return None


def stage_output_producers(stages, upto: int) -> dict:
    """{declared output path -> the name of the FIRST stage before `upto` that declares it}.

    First rather than last on purpose: when two stages claim the same output, the one a later stage's
    input contract should point at is the one that established the path. Built per check rather than
    once because `_resolve_stages` can hand `_run_stages` a different list per attempt."""
    out: dict = {}
    for s in (stages or [])[:max(0, upto)]:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "")
        expect = s.get("expect") if isinstance(s.get("expect"), dict) else {}
        for rel in (expect.get("files") or []):
            out.setdefault(rel, name)
    return out


def materialized_stages(manifest_obj, *, reserved: tuple = ("score",)) -> Optional[list]:
    """The validated PRECEDING stage list from a PARSED `looplab_stages.json` object, or None when it
    declares no usable pipeline / fails validation. Accepts BOTH the wrapped ``{"stages":[...]}`` shape
    `declare_stages` authors AND a bare top-level JSON list (hand-written / write_file / pre-redesign
    manifests): ``dev = obj.get("stages") if isinstance(obj, dict) else obj``. The SINGLE source of
    truth for reading a materialized dev manifest, shared by the eval's `_resolve_stages` (consume
    time) and the repo-Developer's implement-prompt `_materialized_stage_list` (authoring time) — so
    the two can't drift and advertise a pipeline different from the one the eval runs (M7). An invalid
    manifest the eval would DROP to the single command returns None here too. It keeps
    `validate_stages`' fail-closed `allow_env=False`: this is the DEVELOPER's manifest, and a stage
    `env` in it is refused — which, at THIS reader, means the whole manifest is dropped rather than
    the key. The loud version of that refusal is at the authoring tool (`declare_stages`), which is
    where an agent that tries it actually gets told why."""
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
    # Explicit presence check (not `or`) keeps this defensive dict-level function deterministic for
    # snapshots/callers that bypass task admission. New EvalSpec profiles require timeout > 0.
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


# Where `eval.setup`'s Python packages land inside the container so they survive to the eval. Under
# the /work bind (= the node's own workdir on the host), so it is per-node and disappears with it.
_DOCKER_DEPS_DIR = "/work/.looplab-deps"


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
    require_docker_cli("eval")
    root = Path(mount_root).resolve()
    gpu_args = docker_gpu_argv(env, runtime=runtime)
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

    def _build(env: Optional[dict]):
        """The wrap closure over ONE environment. Parametrized (rather than closing over the
        `make_docker_wrap` argument directly) so a stage that declares its own `env` can get a wrap
        that forwards it — see `rebind_env` below. `gpu_args` stays computed ONCE from the engine's
        env: the GPU pin is the engine's, `validate_env_map` refuses a declared CUDA_VISIBLE_DEVICES,
        and re-deriving `--gpus` per stage from a declaration would be exactly the override it refuses.
        """

        def wrap(argv: list[str], host_cwd: str) -> list[str]:
            rel = os.path.relpath(Path(host_cwd).resolve(), root).replace(os.sep, "/")
            if rel == ".." or rel.startswith("../"):     # cwd outside the mounted root -> never escape
                raise ValueError(f"eval cwd {host_cwd!r} is outside the mounted workspace {str(root)!r}")
            cdir = "/work" if rel in (".", "") else f"/work/{rel}"
            # Forward engine-provided env INTO the container: `docker run` does not inherit the host
            # client's environment, so without `-e` the LOOPLAB_EVAL_SEED (and any eval env) never
            # reaches the eval — every confirm seed would read the default seed and the variance gate
            # would collapse. Mirrors DockerSandbox.run's `-e` forwarding.
            # `docker_gpu_env` is the shared choke point that reconciles the GPU/CPU env AND strips
            # secret-named host vars (its SECURITY note) — so both this tier and DockerSandbox.run get the
            # same fail-closed `-e` boundary; nothing extra to filter here.
            envs: list[str] = []
            for k, v in docker_gpu_env(env, gpu_args=gpu_args).items():
                envs += ["-e", f"{k}={v}"]
            # MAKE `setup` STICK. Every call here is its own `docker run --rm`, so a dependency the
            # task's `eval.setup` installs used to die with that container and the eval ran in a pristine
            # one — "installs dependencies before each eval" silently installed nothing, and every node
            # failed on `ModuleNotFoundError` while the operator paid for the repair loop. The mount root
            # is the NODE's workdir (eval_dispatch passes it), so a directory inside it is per-node,
            # already writable by that node's own container, and carries nothing between nodes.
            # `PYTHONUSERBASE` + `PIP_USER` is the mechanism rather than PIP_TARGET/PYTHONPATH because
            # Python adds the user site directory to sys.path natively — no version-dependent path for us
            # to reconstruct and get wrong. Deliberately NOT also overriding PATH: the value would have to
            # be spelled out in full from out here, which would clobber an image that puts its interpreter
            # somewhere non-standard (conda images — the MLE-bench tier — are exactly that), so persisting
            # console scripts is not worth breaking those. KNOWN LIMITS: importable Python packages
            # persist; a setup-installed console script and anything `apt-get` installs do not.
            envs += ["-e", f"PYTHONUSERBASE={_DOCKER_DEPS_DIR}", "-e", "PIP_USER=1"]
            # Resource + privilege hardening comes from the ONE builder both untrusted Docker tiers
            # share (`sandbox.docker_run_argv`), so this command-eval path and the solution.py path
            # cannot drift apart again — they had, and this tier once ran with default caps as root.
            # The two docs (generating-code.md untrusted-tier command, configuration.md
            # sandbox_memory/cpus rows) promise exactly those flags for this tier; the engine threads
            # settings.sandbox_* into `mem`/`cpus` here.
            return docker_run_argv(
                image, network=network, mount_root=str(root), workdir=cdir, runtime=runtime,
                gpu_args=gpu_args, mem=mem, cpus=cpus, env_args=envs,
                extra_mounts=extra) + list(argv)

        wrap._docker = True   # marks a real container wrap -> run_command_eval adds in-container timeout
        wrap._mount_root = str(root)   # host_score guards the held-out labels against the MOUNTED root
        # THE TWO TIERS MUST AGREE. On the subprocess tier a stage's declared `env` reaches the child
        # by being merged into `run_argv`'s env dict; `docker run` inherits NOTHING from the host
        # client, so on this tier the only way in is another `-e` pair — i.e. a different wrap. This
        # is what `_run_stages` asks for when a stage declares `env`, and its absence is what
        # `_stage_wrap` treats as "this wrap cannot carry the declaration", refusing the stage rather
        # than running it in an environment the operator did not declare.
        wrap.rebind_env = lambda extra: _build(merge_env(env, extra))
        return wrap

    return _build(env)


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


def _timed_out(to: bool, rc: int, is_docker: bool) -> bool:
    """Fold a DOCKER timeout exit code into the subprocess-level timed-out flag (doc 25 RA-02).

    Under the container tier the deadline is enforced by coreutils `timeout` INSIDE the container,
    so the host subprocess exits normally and `run_argv` reports `to=False`; the timeout shows up
    only as exit 124 (SIGTERM) or 137 (SIGKILL escalation). Written out at all three run sites, and
    a site that forgets it reports a timed-out eval as an ordinary non-zero failure — which sends
    the Developer to repair code that never got to finish.
    """
    return bool(to or (is_docker and docker_timed_out(rc)))


def _stall_window_for(stall_timeout: Optional[float], budget: float,
                      stall_cap: Optional[float]) -> Optional[float]:
    """The no-output window for one command: an explicit `stall_timeout` wins, else derive it from
    that command's own budget. Two sites spelled this, and they must agree — a staged pipeline whose
    stages derived a different window from the single-command path would kill healthy long stages.
    """
    return stall_timeout if stall_timeout is not None else _stall_window(budget, stall_cap)


@dataclass(frozen=True)
class _EvalExec:
    """The execution knobs both eval paths hand to `run_argv` unchanged (doc 25 RA-02).

    Ten values were threaded identically through the staged loop and the single-command branch, so
    the two run sites read as different code while differing only in what they run and how long for.
    `bound`, `wrap_argv`, `log` and `span` are the closures `run_command_eval` builds once: the
    GPU-flag cap plus the in-container `timeout` prefix, the docker wrap, the live log path, and the
    tracer span.
    """
    wd: Path
    env: Optional[dict]
    cancel: object
    max_output_bytes: int
    grace: float
    is_docker: bool
    wrap: object
    stall_timeout: Optional[float]
    stall_cap: Optional[float]
    bound: object
    wrap_argv: object
    log: object
    span: object


@dataclass
class _EvalRun:
    """What one eval path produced, for the metric read that follows it.

    Every field has a default ON PURPOSE. The tail of `run_command_eval` reads rc/out/err/timed_out
    and the watchdog signals from whichever branch ran, and a branch that left one unbound raised
    UnboundLocalError out of the eval worker — so the node got no terminal event at all. That is the
    `_sig` bug doc 25 RA-02 cites as its evidence, and it was fixed by adding one more pre-binding
    to one branch. Reading the tail off a single object with defaults makes the whole CLASS of
    cross-branch leak unrepresentable instead of fixing its latest instance.

    `early` is a RunResult the caller returns AS IS: a failed stage, or one whose inter-stage check
    raised a concern, is terminal and never reaches the metric read.
    """
    rc: int = 0
    out: str = ""
    err: str = ""
    timed_out: bool = False
    signals: dict = field(default_factory=dict)
    stage_results: Optional[list] = None
    early: Optional[RunResult] = None


# What `_run_stages` reports when a stage declares `env` and the tier it is about to run on cannot
# carry the declaration into the child. Only reachable on a DOCKER wrap that predates `rebind_env`
# (an operator-supplied or test double marked `_docker`): the subprocess tier merges the map into
# `run_argv`'s env dict and always can. The refusal is loud and terminal on purpose — running the
# stage with the declaration silently dropped is how the two tiers would stop agreeing, and the
# failure that would produce (a loader reaching S3 because VS_LOCAL_DATA_ROOT is unset) is exactly the
# one this field exists to prevent, except now with a declaration in the manifest saying it can't happen.
STAGE_ENV_UNSUPPORTED = ("stage {stage!r} declares `env` but this run's container wrap cannot forward "
                         "environment into the container, so the declared variables would silently "
                         "not be set. Refusing rather than running the stage in an environment the "
                         "task did not declare.")


def _stage_wrap(ex: "_EvalExec", stage_env: dict):
    """`(wrap_argv, problem)` for a stage whose declared `env` must reach the child on THIS tier.

    The subprocess tier needs nothing here — its env travels as `run_argv`'s dict — so a run with no
    container wrap (and any non-docker passthrough wrap, which the same dict reaches) keeps
    `ex.wrap_argv` unchanged and is byte-identical to a run without the declaration. A REAL docker
    wrap has to be rebound, because `docker run` inherits none of the host client's environment;
    `make_docker_wrap` grows exactly that seam. A wrap that claims to be docker and cannot be rebound
    is refused (see `STAGE_ENV_UNSUPPORTED`) — never run with the declaration dropped."""
    if not stage_env or not getattr(ex.wrap, "_docker", False):
        return ex.wrap_argv, None
    rebind = getattr(ex.wrap, "rebind_env", None)
    if not callable(rebind):
        return None, STAGE_ENV_UNSUPPORTED
    stage_wrap = rebind(stage_env)
    return (lambda argv, hc: stage_wrap(argv, hc)), None


def _call_stage_check(check_fn, stage: str, tail: str, assertion: str):
    """Call the inter-stage checker, handing it the stage's DECLARED success condition when it has
    one and the callee can take it.

    The signature grew a third argument, and `check_fn` is a duck-typed callback with doubles all
    over the suite and an operator-replaceable engine factory behind it. Calling three-arg
    unconditionally would raise `TypeError` inside `_run_stages`' `except Exception`, which SWALLOWS
    it and returns None — i.e. every two-arg checker would go on being called and silently never
    fire again. That is the "silent narrowing" shape CLAUDE.md warns about, so the arity is decided
    by INSPECTION (the same reasoning as `engine/crash_repair.py::_accepted_kwargs`) and an
    un-inspectable callable keeps the historical two-arg call rather than the new one.

    No assertion declared => the two-arg call, byte-identical to what it always was, so a stage using
    only `check: true` reaches the checker exactly as before.
    """
    if not assertion:
        return check_fn(stage, tail)
    import inspect
    try:
        params = inspect.signature(check_fn).parameters
    except (TypeError, ValueError):
        return check_fn(stage, tail)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()) or "expect" in params:
        return check_fn(stage, tail, expect=assertion)
    positional = [p for p in params.values()
                  if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    if len(positional) >= 3 or any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params.values()):
        return check_fn(stage, tail, assertion)
    return check_fn(stage, tail)


def _run_stages(stages: list, ex: _EvalExec, *, timeout: float, start_stage: Optional[str],
                metric: dict, eval_started: float, check_fn) -> _EvalRun:
    """Run the declared stage pipeline in order; the LAST stage's output feeds the metric read.

    Multi-stage pipeline (data_prep → train → eval): run each stage in ORDER in the SAME workdir
    (artifacts persist across stages), each in its own span + <name>.log, tracking pass/fail. The
    FIRST failure stops the pipeline and returns "failed at stage <name>" — so a crash in `train`
    is pinpointed (not hidden behind an opaque single command) and the good earlier stages' outputs
    stay on disk for a later stage-scoped re-run. The LAST stage's stdout carries the metric.
    Stage-scoped re-run (Phase 2): `start_stage` re-runs the pipeline FROM that stage, reusing the
    earlier stages' on-disk artifacts (the checkpoint `train` wrote survives in the workdir). So a
    crashed `eval` is fixed without paying to re-`train`. Stages before it are marked "reused".
    An UNKNOWN name leaves `_run_from` at 0 — a FULL re-run, the fail-safe direction, since
    reusing on a typo would score a stale artifact. Pinned end-to-end (reuse, the zero-work
    markers, and that fallback) by tests/test_command_eval.py::
    test_start_stage_reuses_earlier_stages_instead_of_paying_for_them_again.

    SUCCESS CONTRACT (`expect`): a stage that exits 0 is then held to whatever it DECLARED. The
    technical half runs first and unconditionally (`verify_stage_artifacts` — declared artifacts
    exist, are non-empty, and were written by THIS run of the stage); only if that passes is the
    declared `assert` sentence handed to the LLM checker, which is the half that can see "9,364 of
    764,676". Order matters and is not cosmetic: the cheap deterministic check must not be gated
    behind a model call that may be unwired, rate-limited, or simply absent — an operator running
    without a reflect client still gets artifact verification, which is most of the value for none
    of the cost.

    DECLARED ENVIRONMENT (`env`): the per-stage overlay on top of `ex.env` (which already carries the
    run- and task-level layers). It is resolved per stage rather than hoisted, because it is the one
    stage field that changes what the CHILD PROCESS is, and both tiers have to be given it the way
    that tier accepts — see `_stage_wrap`.
    """
    _run_from = 0
    if start_stage:
        for _i, _s in enumerate(stages):
            if str(_s.get("name")) == str(start_stage):
                _run_from = _i
                break
    run = _EvalRun(stage_results=[])
    stage_results = run.stage_results
    for _i, _stg in enumerate(stages):
        _sname = str(_stg.get("name") or f"stage{_i}")
        _scmd = list(_stg.get("command") or [])
        if _i < _run_from:
            # Reused: an earlier repair attempt already ran this stage and its on-disk artifacts
            # (e.g. the train checkpoint) are kept, so it does NOT re-run. Still emit a zero-work
            # marker span so the trace SHOWS the stage on this re-eval (labeled "reused") instead of
            # the band silently vanishing — otherwise the user sees no Train span after a repair.
            with ex.span(_sname, kind="operation", stage=_sname, reused=True):
                pass
            stage_results.append({"name": _sname, "status": "reused", "exit_code": 0, "seconds": 0.0})
            continue
        _sto = finite_timeout(_stg.get("timeout", timeout), timeout)
        if not _scmd:
            continue
        # THE INPUT CONTRACT, before anything is spent. A stage whose declared input is not there
        # cannot succeed, and every second it runs before discovering that is a second bought at GPU
        # prices — v5 node 0 spent 76 minutes on a pipeline whose scorer read a directory the trainer
        # never wrote to. The refusal is shaped exactly like the `expect` one (a stage row, an early
        # return, `failed_stage` set) so it flows through the SAME repair loop and stage-scoped re-run
        # with no new mid-loop vocabulary — the status is different only because the remedy is.
        # `isinstance`, not `or []`: `run_command_eval(stages=...)` is public, and a raw
        # `"needs": "ckpt.pt"` string must degrade to "no contract" rather than iterate per character.
        _needs = _stg.get("needs") if isinstance(_stg.get("needs"), list) else []
        _input_problem = (verify_stage_inputs(_needs, str(ex.wd), stage=_sname,
                                              producers=stage_output_producers(stages, _i))
                          if _needs else None)
        if _input_problem:
            stage_results.append({"name": _sname, "status": "needs_failed", "exit_code": 0,
                                  "seconds": 0.0, "concern": str(_input_problem)[:700]})
            run.early = RunResult(
                exit_code=0, stdout=run.out, metric=None, timed_out=False,
                stderr=f"stage '{_sname}' failed its declared input contract: {_input_problem}",
                stages=stage_results, failed_stage=_sname)
            return run
        # THE DECLARED ENVIRONMENT, resolved for THIS stage. `ex.env` already carries the run- and
        # task-level layers (the engine composed them into the process env before the eval started);
        # this is the per-stage overlay, applied last because it is the most specific. `isinstance`
        # for the same reason `needs` uses it: `run_command_eval(stages=...)` is public, and a raw
        # string must degrade to "no declaration" rather than being iterated.
        _senv_decl = _stg.get("env") if isinstance(_stg.get("env"), dict) else {}
        _senv = merge_env(ex.env, _senv_decl) if _senv_decl else ex.env
        _swrap_argv, _env_problem = _stage_wrap(ex, _senv_decl)
        if _env_problem:
            stage_results.append({"name": _sname, "status": "env_unsupported", "exit_code": 0,
                                  "seconds": 0.0, "concern": _env_problem.format(stage=_sname)})
            run.early = RunResult(
                exit_code=2, stdout=run.out, metric=None, timed_out=False,
                stderr=_env_problem.format(stage=_sname),
                stages=stage_results, failed_stage=_sname)
            return run
        _t0 = time.monotonic()
        # WALL-CLOCK stage start, beside the monotonic one that measures duration: the freshness half
        # of the success contract compares against `st_mtime`, which is wall clock, and monotonic
        # seconds are not comparable with it (on this box they differ by the machine's uptime — every
        # declared artifact would read as stale). Taken here, before the command runs, so an artifact
        # written by a PREVIOUS attempt of this same stage cannot pass as this one's.
        _w0 = time.time()
        with ex.span(_sname, kind="operation", sandboxed=bool(ex.wrap), stage=_sname) as _sh:
            # Live-band anchor: a training subprocess emits NO child LLM/tool spans, and this stage's
            # operation span is written to spans.jsonl only on CLOSE (tracing.Tracer.span), so without
            # a live child the trace view shows nothing for the whole ~hour of training and the
            # "Train"/"Evaluate" block appears only at the end. A zero-work child span carries the
            # phase stamp (see tracing._phase_ctx), which the live view bands under this stage the
            # instant it opens — the intended live mechanism, just given something to anchor on.
            with ex.span("stage_started", kind="tool", stage=_sname):
                pass
            # health_check: a declared stage is where TRAINING runs (often multi-hour). Watch its live
            # stdout for a diverged (non-finite) loss and tree-kill early instead of burning the whole
            # timeout on a model that can no longer learn — the killed stage then fails with a DIVERGED
            # marker the agent reads. Off for the short scorer `cmd` (it is not a training loop).
            # `run.signals` is the AUTHENTICATED watchdog verdict. `err` mixes the watchdog marker with
            # the CANDIDATE's own stderr, so `STALL_SENTINEL in err` is forgeable — and this is the
            # only health_check=True path, where the DIVERGE watchdog forces a non-zero exit against
            # the candidate's will. A forged stall marker turned that fail-closed verdict back into
            # an accepted self-reported metric; read the out-of-band flag instead.
            run.signals = {}
            run.rc, run.out, run.err, run.timed_out = run_argv(
                _swrap_argv(ex.bound(_scmd, _sto), str(ex.wd)), ex.wd, _sto + ex.grace, _senv,
                ex.max_output_bytes, ex.cancel,
                log_path=ex.log(f"{_sname}.log"), health_check=True,
                stall_timeout=_stall_window_for(ex.stall_timeout, _sto, ex.stall_cap),
                signals=run.signals)
            run.timed_out = _timed_out(run.timed_out, run.rc, ex.is_docker)
            if _sh is not None:
                _sh.set_many(exit_code=run.rc, timed_out=run.timed_out, stage=_sname)
        _status = "timeout" if run.timed_out else ("ok" if run.rc == 0 else "fail")
        stage_results.append({"name": _sname, "status": _status, "exit_code": run.rc,
                              "seconds": round(time.monotonic() - _t0, 3)})
        if _status != "ok":
            # STALL-SALVAGE parity with the single-command path: that path reads the metric whenever
            # the run was not a hard timeout and reports the same authenticated `_sig` verdict, so
            # evaluate.py's gate (`metric is not None and not timed_out and (exit==0 or stalled)`)
            # can rescue a train+eval that printed its metric then hung on teardown (a wedged CUDA
            # finalize, a distributed barrier) and was tree-killed by the stall watchdog. Mirror it
            # for the FINAL (metric-producing) stage — an earlier stage's failure has no metric to
            # salvage, and the read is only attempted when it wasn't a hard timeout, via the call-site
            # `not to` gate below (the same gate the single-command path uses). A plain crash reads a
            # value too but stays unsalvaged (stalled False, exit!=0), exactly as in single-command mode.
            _salvaged = (read_metric(run.out, str(ex.wd), metric, wrap=ex.wrap, since=eval_started)
                         if (_i == len(stages) - 1 and not run.timed_out) else None)
            run.early = RunResult(
                exit_code=run.rc, stdout=run.out, stderr=f"stage '{_sname}' failed:\n{run.err}",
                metric=_salvaged, timed_out=run.timed_out, stages=stage_results,
                failed_stage=_sname, stalled=_salvageable_stall(run.signals),
                diverged=bool(run.signals.get("diverged")))
            return run
        # THE SUCCESS CONTRACT, technical half. Runs on every stage that declared `expect`, needs no
        # model, and fails the stage exactly the way a non-zero exit does — same `failed_stage`, same
        # early return — so it flows into the EXISTING repair loop, the EXISTING `_safe_reuse_start`
        # and the EXISTING stage-scoped re-run with no new mid-loop vocabulary. That is the answer to
        # the BACKLOG's open question "what does verification failure DO": the stage FAILED, because a
        # stage that did not produce what it declared did not succeed, whatever it exited with.
        # `isinstance`, not `or {}`: every stage reaching a RUN has been through `validate_stages`,
        # but `run_command_eval(stages=...)` is a public entry point a library caller can hand raw
        # dicts to, and `"expect": "hard_negs.pkl"` would then raise AttributeError out of the eval
        # WORKER — no node terminal, and the run re-dies on every resume. Same failure class as the
        # unregistered metric-reader path slot in CLAUDE.md; a malformed declaration must degrade to
        # "no contract", never take down the run.
        _expect = _stg.get("expect") if isinstance(_stg.get("expect"), dict) else {}
        _artifact_problem = (verify_stage_artifacts(_expect, str(ex.wd), _w0, stage=_sname)
                             if _expect.get("files") else None)
        if _artifact_problem:
            stage_results[-1]["status"] = "expect_failed"
            # 700, not the 300 the AGENTIC concern below gets. That one is model prose and bounding
            # it is the point; this one is a fixed, code-owned sentence that is 375 characters before
            # the near-miss path is appended — at 300 the row lost its own remediation ("Do not
            # delete the declaration to make this pass") and would now lose the one part that says
            # the stage actually worked. A cap that truncates the answer is not a bound, it is a bug.
            stage_results[-1]["concern"] = str(_artifact_problem)[:700]
            # The floor this contract was checked against, so a re-check of a CORRECTED declaration
            # is held to the identical one — see `EXPECT_SINCE_KEY`.
            stage_results[-1][EXPECT_SINCE_KEY] = _w0
            # The DECLARATION that failed, verbatim, so a re-check can establish that the repair
            # actually reached IT — see `EXPECT_DECLARED_KEY`.
            stage_results[-1][EXPECT_DECLARED_KEY] = list(_expect.get("files") or [])
            run.early = RunResult(
                exit_code=0, stdout=run.out, metric=None, timed_out=False,
                stderr=f"stage '{_sname}' failed its declared artifact contract: {_artifact_problem}",
                stages=stage_results, failed_stage=_sname)
            return run
        # Phase 3 — optional inter-stage verify: a stage flagged `"check": true` hands its output tail
        # to an agentic checker (Researcher/Developer) BEFORE the next stage runs; a returned concern
        # stops the pipeline early ("failed verification") so a bad artifact (e.g. a diverged train)
        # doesn't silently feed the next stage. No check_fn / no flag => never called (zero overhead).
        #
        # A declared `expect.assert` IMPLIES the gate. Requiring `check: true` as well would mean a
        # stage that states its own success condition is silently never held to it — which is the
        # whole defect, re-created one field over. `check: true` alone keeps its historical meaning:
        # the contract-free sanity read, whose prompt still forbids quality judgements.
        _assertion = str(_expect.get("assert") or "")
        if (_stg.get("check") or _assertion) and check_fn is not None:
            try:
                _concern = _call_stage_check(check_fn, _sname, run.out[-4000:], _assertion)
            except Exception:  # noqa: BLE001 — a checker failure must not crash the eval
                _concern = None
            if _concern:
                stage_results[-1]["status"] = "check_failed"
                stage_results[-1]["concern"] = str(_concern)[:300]
                run.early = RunResult(
                    exit_code=0, stdout=run.out, metric=None, timed_out=False,
                    stderr=f"stage '{_sname}' failed verification: {_concern}",
                    stages=stage_results, failed_stage=_sname)
                return run
    # all stages passed -> the LAST stage's `out`/`rc`/`to` flow into read_metric below.
    return run


def _run_single(command: list, ex: _EvalExec, *, timeout: float) -> _EvalRun:
    """Run the eval as ONE command; its stdout feeds the metric read."""
    run = _EvalRun()
    with ex.span("command", sandboxed=bool(ex.wrap)) as _h:
        # Live-band anchor (see the multi-stage branch): flush a child the instant the single eval
        # command starts, so the "Evaluate" block shows live instead of only when the command ends.
        with ex.span("stage_started", kind="tool", phase="evaluate"):
            pass
        # A single-command RepoTask eval IS the training (train->eval in one process, often
        # multi-hour), not just a short scorer — so it gets the STALL watchdog too (health_check
        # stays off here: the NaN scan is for declared training stages, and a scorer may legitimately
        # print 'nan'; the stall watchdog is output-based and safe for any command).
        # `run.signals` is the AUTHENTICATED watchdog verdict, exactly as the staged branch above uses
        # it. `err` mixes the watchdog's marker with the CANDIDATE's own stderr, so
        # `STALL_SENTINEL in err` is forgeable: a solution that prints its metric, echoes the
        # marker to stderr and exits non-zero used to report stalled=True, and evaluate.py's
        # salvage gate (`metric is not None and not timed_out and (exit == 0 or stalled)`) then
        # accepted a CRASHED run's self-reported score. This path is the common RepoTask eval, so
        # it needs the out-of-band flag just as much as the staged one.
        run.signals = {}
        run.rc, run.out, run.err, run.timed_out = run_argv(
            ex.wrap_argv(ex.bound(command, timeout), str(ex.wd)), ex.wd, timeout + ex.grace, ex.env,
            ex.max_output_bytes, ex.cancel,
            log_path=ex.log("eval.log"),
            stall_timeout=_stall_window_for(ex.stall_timeout, timeout, ex.stall_cap),
            signals=run.signals)
        run.timed_out = _timed_out(run.timed_out, run.rc, ex.is_docker)
        if _h is not None:
            _h.set_many(exit_code=run.rc, timed_out=run.timed_out)
    return run


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
                     stall_timeout: Optional[float] = None,
                     stall_cap: Optional[float] = None,
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
    def _log(name):
        return str(Path(log_dir) / name) if log_dir else None

    def _sp(name, **attrs):                              # child span when a tracer is wired
        return tracer.span(name, **attrs) if tracer is not None else nullcontext(None)

    # Only a REAL docker wrap gets the in-container `timeout` prefix (a non-docker passthrough
    # wrap, e.g. in tests, must not get a host `timeout` prepended — that is timeout.exe on
    # Windows and would break the command).
    is_docker = bool(getattr(wrap, "_docker", False))

    # GPU-pin reconciliation gate: the engine pins a PARALLEL eval to exactly ONE GPU by setting a
    # single-index CUDA_VISIBLE_DEVICES in `env` (engine/resources.py::_acquire_gpus). When it did, cap any
    # explicit multi-device request in the command down to one device (below) — the subprocess sees
    # only 1 GPU, so `--gpus 2`/`--devices 2`/`--gpus 0,1` would otherwise crash (pytorch-lightning
    # pick_multiple_gpus), including the PROTECTED score command the agent cannot edit. No-op when
    # unpinned (env None, or CVD names multiple devices — a serial run legitimately has the whole box).
    _cvd = (env or {}).get("CUDA_VISIBLE_DEVICES")
    _pinned_one_gpu = bool(_cvd) and "," not in str(_cvd)

    def _bound(argv, secs):
        # Reconcile multi-GPU flags FIRST (before the docker wrap, so docker's own `--gpus` is
        # untouched), then apply the docker `timeout` prefix. Under the docker wrap, self-limit the
        # container with coreutils `timeout` so a runaway exits from INSIDE (+ --rm cleanup) even if
        # the host kills the `docker run` client — killing the CLI does not stop the daemon-owned
        # container.
        argv = cap_gpu_flags(list(argv)) if _pinned_one_gpu else list(argv)
        return (["timeout", "-k", "5", str(max(1, int(secs)))] + argv) if is_docker else argv

    grace = 15.0 if is_docker else 0.0
    # A Docker wrap creates a fresh `docker run --rm` for every call below, so setup and eval do NOT
    # share a container: what makes the setup-installed packages reach the eval is the shared
    # `PYTHONUSERBASE` under the /work bind that `make_docker_wrap` exports (see _DOCKER_DEPS_DIR).
    if setup:
        swd = Path(setup_cwd).resolve() if setup_cwd else wd
        swd.mkdir(parents=True, exist_ok=True)
        with _sp("setup", sandboxed=bool(wrap)):
            rc, out, err, to = run_argv(_w(_bound(setup, setup_timeout), str(swd)), swd,
                                         setup_timeout + grace, env, max_output_bytes, cancel,
                                         log_path=_log("setup.log"))
        to = _timed_out(to, rc, is_docker)
        if rc != 0 or to:
            return RunResult(exit_code=rc, stdout=out, stderr="setup failed:\n" + err,
                             metric=None, timed_out=to)
    # Freshness gate (§6.3): the eval's OWN work starts now (after setup, which installs deps, not
    # results). Every FILE-based metric/constraint reader below must find a source file written at/after
    # this instant — a stale artifact left by a prior attempt in a reused workdir is rejected, so a
    # command that produced no new output can't promote an old metric. Captured before the child so its
    # writes are strictly newer. stdout readers are inherently this-run and unaffected.
    _eval_started = time.time()
    _ex = _EvalExec(wd=wd, env=env, cancel=cancel, max_output_bytes=max_output_bytes, grace=grace,
                    is_docker=is_docker, wrap=wrap, stall_timeout=stall_timeout,
                    stall_cap=stall_cap, bound=_bound, wrap_argv=_w, log=_log, span=_sp)
    # ONE object carries what either path produced into the metric read below, so the tail can never
    # read a name the branch that ran happened not to bind (doc 25 RA-02).
    _run = (_run_stages(stages, _ex, timeout=timeout, start_stage=start_stage, metric=metric,
                        eval_started=_eval_started, check_fn=check_fn)
            if stages else _run_single(command, _ex, timeout=timeout))
    if _run.early is not None:
        return _run.early
    rc, out, err, to, _sig = _run.rc, _run.out, _run.err, _run.timed_out, _run.signals
    stage_results = _run.stage_results
    with _sp("read_metric", kind=metric.get("kind", "stdout_json")):
        m = read_metric(out, str(wd), metric, wrap=wrap, since=_eval_started) if not to else None
    # F13 stage-reuse: on a stage-scoped re-run (`start_stage`), the earlier stages are DELIBERATELY
    # reused — their on-disk artifacts keep their prior-eval mtime — so a constraint / extra / cross-check
    # reader that points at a reused stage's file is legitimately "old" and the `_eval_started` gate would
    # falsely reject it (a spurious violation → the node is wrongly excluded from best). Relax those
    # secondary readers to no freshness gate in that mode. The PRIMARY metric stays strict: it comes from
    # the final (re-RUN) stage, so it MUST be freshly produced — a no-op stage can't promote an old value.
    # Pinned in BOTH directions (relaxed under start_stage, strict without it) by
    # tests/test_command_eval.py::
    # test_a_reused_stages_artifact_still_feeds_the_secondary_readers_but_only_under_start_stage.
    _reader_since = None if start_stage else _eval_started
    drift = None
    if enforce_drift and cross_check and m is not None:
        validate_cross_check(cross_check)
        cross = read_metric(out, str(wd), cross_check, wrap=wrap, since=_reader_since)
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
                 if (v := read_metric(out, str(wd), spec, wrap=wrap, since=_reader_since)) is not None}
                if (metrics and not to) else {})   # a MISSED reader (None) must not erase a
    #                                                successfully auto-captured value of the same name
    # Auto-capture: every other numeric key on the metric's own JSON line is also reported (no config
    # needed), so an experiment that prints {"metric": x, "recall@10": y, ...} surfaces them all. A
    # declared spec wins over the auto-captured value of the same name.
    auto = (json_line_extras(out, metric.get("key", "metric"))
            if (not to and metric.get("kind", "stdout_json") == "stdout_json") else {})
    extra = ({**auto, **declared} or None)
    viol = (_violations(out, str(wd), constraints, wrap, since=_reader_since)
            if (constraints and not to and m is not None) else None)
    # Intra-node sweep: a RepoTask command may emit the same `{"trials": [...]}` stdout line; carry
    # it so the engine can collapse it to the node's best metric (no eval_spec change required).
    trials = json_line_trials(out) if not to else None
    return RunResult(exit_code=rc, stdout=out, stderr=err, metric=m, timed_out=to, drift=drift,
                     extra_metrics=extra, violations=(viol or None), trials=trials,
                     stages=stage_results, stalled=_salvageable_stall(_sig),
                     diverged=bool(_sig.get("diverged")))

