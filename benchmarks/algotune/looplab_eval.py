#!/usr/bin/env python3
"""LoopLab <-> AlgoTune evaluation bridge.

Scores a LoopLab-produced ``solver.py`` with **AlgoTune's own evaluator**, on this
machine, and prints the speedup as JSON on stdout for LoopLab's ``stdout_json``
metric reader.

No scoring logic lives here. The bridge moves one file into the layout
``evaluate_results.py`` expects (``results/<model>/<task>/solver.py``), invokes that
script for exactly one ``(model, task)`` pair, and reads the number back. Whatever
AlgoTune computes is what LoopLab optimises:

    speedup = baseline_ms / optimized_ms      (100 % instance validity required)

Both times are measured locally in the same pass, so the ratio self-normalises
against hardware — which is what makes re-timing other agents' shipped solvers on
this box a fair comparison rather than a cross-machine guess.

PARITY — the reason ``--baseline-cache`` exists
-----------------------------------------------
AlgoTune's own agent loop pays the reference ("oracle") timing **once per run**:
``BaselineManager`` holds it in-process and every later ``eval`` command is cheap.
Measured on an RTX 5090 box (2026-08-19), that one-time cost is ~150 instances at
~3 s each, i.e. **about 30 minutes for a single task**.

A bridge that shells out once per candidate re-pays that on *every* node. The
resulting slowdown would be a property of this wiring, not of the agent under test,
and it cannot be corrected by granting more wall-clock because the overhead scales
with node count.

So the bridge caches the per-task baseline on first measurement and reuses it, which
is precisely what ``BaselineManager`` does inside an AlgoTuner run. This is parity
restoration, not a protocol deviation — but it is a deviation from a naive reading
of the harness, so it must appear in any methods note that accompanies published
numbers. Pass ``--no-cache`` to force a full re-measurement.

Usage
-----
    python looplab_eval.py --algotune-root /path/to/AlgoTune \\
                           --task svm --model LoopLab --solver solver.py

See ``benchmarks/algotune/README.md`` for the full workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Where the per-task baseline timings are remembered between invocations. Kept beside
# the bridge rather than inside the AlgoTune checkout so a `git clean` of the upstream
# tree cannot silently discard the parity cache mid-campaign.
DEFAULT_CACHE = Path(__file__).resolve().parent / ".baseline_cache.json"
# The per-INSTANCE reference timings (the cache that saves the WALL CLOCK), as opposed to
# DEFAULT_CACHE above, which holds the aggregate (the cache that stabilises the DENOMINATOR).
# The per-instance one is written by `patch_baseline_cache.py`, which patches AlgoTune itself.
DEFAULT_TIMES_DIR = Path(__file__).resolve().parent / ".baseline_times"


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing or corrupt cache must never fail a run
        return {}


def _save_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:  # noqa: BLE001 - the measurement already succeeded; caching is best-effort
        pass


_SPEEDUP_KEYS = ("final_speedup", "speedup", "avg_speedup")


def _coerce_speedup(value) -> float | None:
    """`evaluate_results.py` writes the number as a STRING, and writes words for a non-number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None          # "N/A" / "Error" are the harness's own words for "no number"


def _find_result(node, task: str, model: str) -> dict | None:
    """Locate this (task, model) result anywhere in `evaluate_summary.json`.

    THE SHAPE THIS ACTUALLY HAS, measured 2026-08-19 against a real run:

        {"discrete_log": {"BV4": {"final_speedup": "0.9963"}}}

    Note what is NOT there: no `task_name`, no `speedup`, no `baseline_time_ms`, and the value is a
    STRING. The first version of this function looked for `node.get("task_name") == task and
    "speedup" in node` — fields nothing writes — so it returned None on a summary that had just been
    written successfully, and the bridge reported `speedup: 0.0` for a solver the evaluator had
    scored at 0.9963. It could never have returned a number for any task, on any node, for the whole
    campaign, and the failure looked exactly like a wrong solver.

    So the documented shape is read FIRST and explicitly. The tolerant walk stays underneath it
    because upstream has reshaped this file before — but a walk is a fallback, never the primary
    reader: a reader that only ever guesses cannot go red when the guess stops matching.
    """
    if isinstance(node, dict):
        entry = node.get(task)
        if isinstance(entry, dict):
            row = entry.get(model)
            if isinstance(row, dict):
                for key in _SPEEDUP_KEYS:
                    if key in row:
                        return dict(row)
            # A model key we did not predict (upstream normalizes names): accept a UNIQUE row.
            rows = [dict(v) for v in entry.values()
                    if isinstance(v, dict) and any(k in v for k in _SPEEDUP_KEYS)]
            if len(rows) == 1:
                return rows[0]
    return _walk_for_result(node, task)


def _walk_for_result(node, task: str) -> dict | None:
    """Fallback for a reshaped summary: any dict that names this task and carries a speedup."""
    if isinstance(node, dict):
        if node.get("task_name") == task and any(k in node for k in _SPEEDUP_KEYS):
            return node
        for value in node.values():
            found = _walk_for_result(value, task)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _walk_for_result(value, task)
            if found is not None:
                return found
    return None


def _rules_violation(root: Path, solver: Path) -> str:
    """AlgoTune's own verdict on this candidate, or "" when it is clean.

    Their validator, imported from their checkout — not a reimplementation and not a copied list.
    The rule set is theirs to change; a second copy here would go stale in the direction that
    silently permits, which is the direction that produces an incomparable number.

    A validator that cannot be imported returns "" and says so on stderr: refusing to score every
    node because their layout moved would turn an arena change into a campaign of zeros, and this
    check is a fairness guarantee, not a safety boundary (the safety boundary is the probe's kernel
    rung, which does not depend on it).
    """
    sys.path.insert(0, str(root))
    try:
        from AlgoTuner.security.code_validator import check_code_for_tampering
    except Exception as exc:                    # noqa: BLE001 — see the docstring
        print(f"looplab_eval: --enforce-rules asked for, but AlgoTune's validator did not import "
              f"({exc!r}); scoring WITHOUT the rule check", file=sys.stderr)
        return ""
    finally:
        if sys.path and sys.path[0] == str(root):
            sys.path.pop(0)
    try:
        return str(check_code_for_tampering(solver.read_text(encoding="utf-8")) or "")
    except SyntaxError as exc:
        # Their validator re-raises a syntax error rather than reporting it, and a solver that does
        # not parse is the EVALUATOR's failure to report, not a rules violation.
        print(f"looplab_eval: solver does not parse ({exc}); leaving it to the evaluator",
              file=sys.stderr)
        return ""



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path,
                    help="Path to the AlgoTune checkout (the dir holding scripts/ and results/).")
    ap.add_argument("--task", required=True, help="AlgoTune task name, e.g. 'svm'.")
    ap.add_argument("--model", default="LoopLab",
                    help="Directory name under results/ this candidate is filed as.")
    ap.add_argument("--solver", default="solver.py", help="Candidate solver to score.")
    ap.add_argument("--baseline-cache", type=Path, default=DEFAULT_CACHE,
                    help=f"Per-task baseline cache (default: {DEFAULT_CACHE}).")
    ap.add_argument("--no-cache", action="store_true",
                    help="Ignore and do not write the aggregate baseline cache; recompute the ratio "
                         "from each run's freshly measured baseline.")
    ap.add_argument("--baseline-times-dir", type=Path, default=DEFAULT_TIMES_DIR,
                    help="Where patch_baseline_cache.py keeps the per-instance reference timings. "
                         "Informational here; the patch owns that path.")
    ap.add_argument("--subset", choices=("train", "test"), default="train",
                    help="Dataset half to score on. Default TRAIN, mirroring AlgoTuner's own agent, "
                         "which iterates on train and touches test only for its final number — "
                         "scoring every node on test would let this arm optimise against the graded "
                         "split while the other arm does not. Requires patch_eval_subset.py.")
    ap.add_argument("--timeout", type=int, default=7200, help="Seconds to allow the evaluator.")
    ap.add_argument("--enforce-rules", action="store_true",
                    help="Run AlgoTune's OWN solver validator (AlgoTuner.security.code_validator) "
                         "on the candidate before scoring, and refuse to score a violation. OFF by "
                         "default: these are THIS arena's rules, not LoopLab's, and a LoopLab task "
                         "that is not an AlgoTune arm must not inherit them.")
    args = ap.parse_args()

    root: Path = args.algotune_root.resolve()
    evaluator = root / "scripts" / "evaluate_results.py"
    if not evaluator.exists():
        print(json.dumps({"speedup": 0.0, "error": f"no evaluate_results.py under {root}"}))
        return 0

    src = Path(args.solver).resolve()
    if not src.exists():
        print(json.dumps({"speedup": 0.0, "error": f"no solver at {src}"}))
        return 0

    if args.enforce_rules:
        violation = _rules_violation(root, src)
        if violation:
            # NOT a score of zero, and not a crash to repair: this candidate was never eligible.
            # Arm A cannot produce such a solver at all — AlgoTune validates at EDIT time and simply
            # refuses to write the file (`editor/editor_functions.py:1377`), so its agent learns the
            # rule and loses nothing. Our Developer writes through LoopLab's own tools, which that
            # validator never sees, so without this the two arms would be playing by different rules
            # about what may be SUBMITTED — and a speedup obtained with a primitive the other arm was
            # forbidden is not a comparable number.
            print(json.dumps({"speedup": None, "rules_violation": violation,
                              "error": f"rules_violation: {violation}"}))
            return 2

    # A PER-INVOCATION model name, because LoopLab evaluates nodes CONCURRENTLY (`eval_parallel`).
    # With the fixed `--model LoopLab`, two nodes copied their solvers over one another in
    # `results/LoopLab/<task>/`, and each `summary.unlink()` deleted the summary the other was about
    # to read -- so a node could record its sibling's speedup as its own, or find no summary and
    # score 0.0. The suffix keeps the runs apart; `--model` still names the family for reporting.
    model_dir = f"{args.model}-{os.getpid()}"
    dest_dir = root / "results" / model_dir / args.task
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / "solver.py")

    # The summary path is per-invocation for the same reason: `evaluate_results.py` writes one
    # shared `reports/evaluate_summary.json`, so concurrent bridges would read each other's.
    summary = root / "reports" / f"evaluate_summary.{os.getpid()}.json"
    if summary.exists():
        summary.unlink()

    # The evaluator is invoked DIRECTLY. The persistent baseline cache is applied by patching
    # `BaselineManager` on disk (`patch_baseline_cache.py`), NOT by wrapping it from here: measured
    # 2026-08-19, importing AlgoTuner in this process before the evaluator builds its worker pool
    # crashed every evaluation ("A process in the process pool was terminated abruptly"), while the
    # identical run invoked directly returned 0.9963x.
    argv = [sys.executable, str(evaluator), "--models", model_dir, "--tasks", args.task,
            "--output", str(summary)]

    # The split is carried in the ENVIRONMENT rather than as a flag: `evaluate_results.py` hardcodes
    # it at three sites and has no argument for it, so `patch_eval_subset.py` reads this name. An
    # unpatched checkout ignores it and scores on test, which is upstream's own behaviour.
    env = dict(os.environ, ALGOTUNE_EVAL_SUBSET=args.subset)

    started = time.time()
    try:
        proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True,
                              timeout=args.timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        # EVERY other failure path here prints `{"speedup": 0.0, "error": ...}` so LoopLab's
        # `stdout_json` reader gets a number. This one used to let the exception escape, so the one
        # failure the `--timeout` flag exists for was the only one that printed NOTHING -- the node
        # then recorded "no metric" rather than a scored-zero, which `metric_salvage` DISCARDS
        # instead of counting as a failed solver. A timed-out solver is a wrong solver, not a
        # missing measurement.
        print(json.dumps({"speedup": 0.0, "eval_seconds": round(time.time() - started, 1),
                          "subset": args.subset,
                          "error": f"evaluator exceeded --timeout {args.timeout}s",
                          "stderr_tail": (exc.stderr or b"")[-1000:].decode("utf-8", "replace")
                          if isinstance(exc.stderr, bytes) else (exc.stderr or "")[-1000:]}))
        return 0
    elapsed = round(time.time() - started, 1)

    out: dict[str, Any] = {"speedup": 0.0, "eval_seconds": elapsed, "subset": args.subset}

    if not summary.exists():
        out["error"] = "evaluate_summary.json not produced"
        out["stderr_tail"] = proc.stderr[-1000:]
        print(json.dumps(out))
        return 0

    try:
        record = _find_result(json.loads(summary.read_text(encoding="utf-8")),
                              args.task, model_dir)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"unreadable summary: {type(exc).__name__}: {exc}"
        print(json.dumps(out))
        return 0

    if record is None:
        out["error"] = f"no record for task {args.task!r} in evaluate_summary.json"
        out["stderr_tail"] = proc.stderr[-1000:]
        print(json.dumps(out))
        return 0

    speedup = next((_coerce_speedup(record[k]) for k in _SPEEDUP_KEYS if k in record), None)
    # These two are absent from the shape this file actually has; they are read defensively so the
    # aggregate cache below still works if upstream restores them.
    baseline_ms = record.get("baseline_time_ms")
    optimized_ms = record.get("optimized_time_ms")

    # Parity: remember this task's baseline the first time it is measured, and report the
    # cached value on later calls so an out-of-process bridge does not re-pay a cost the
    # in-process reference loop pays once. The SOLVER time is always freshly measured.
    cache_key = f"{args.task}"
    if not args.no_cache:
        cache = _load_cache(args.baseline_cache)
        cached = cache.get(cache_key)
        if baseline_ms is not None and cached is None:
            cache[cache_key] = {"baseline_time_ms": baseline_ms, "measured_at": elapsed}
            _save_cache(args.baseline_cache, cache)
            out["baseline_source"] = "measured (now cached)"
        elif cached is not None:
            out["baseline_source"] = "cache"
            out["baseline_cached_ms"] = cached["baseline_time_ms"]
            # Recompute against the cached baseline so every node is scored on the same
            # denominator; a denominator that drifts per node is not a comparable metric.
            if optimized_ms:
                speedup = float(cached["baseline_time_ms"]) / float(optimized_ms)
        else:
            # NOT "the baseline is missing" -- the speedup above came through fine and is the
            # harness's own. This branch means only that the RECORD carries no `baseline_time_ms`
            # for this bridge to cache, which is the normal shape (see the note above the reads),
            # so the aggregate cache never engages. The old label read as a failure and cost a real
            # investigation on 2026-08-20: a node scoring 0.0 was diagnosed as "no baseline" when
            # the actual cause was an empty working set, three layers away.
            out["baseline_source"] = "in-harness (record exposes no baseline_time_ms to cache)"

    out["speedup"] = float(speedup) if speedup is not None else 0.0
    if baseline_ms is not None:
        out["baseline_time_ms"] = baseline_ms
    if optimized_ms is not None:
        out["optimized_time_ms"] = optimized_ms
    for key in ("is_valid", "success", "error_message"):
        if record.get(key) is not None:
            out[key] = record[key]

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
