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
DEFAULT_TIMES_DIR = Path(__file__).resolve().parent / ".baseline_times"
RUNNER = Path(__file__).resolve().parent / "run_evaluator.py"


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


def _find_result(node: Any, task: str) -> dict[str, Any] | None:
    """Locate this task's result record anywhere in evaluate_summary.json.

    The summary's nesting has changed shape across AlgoTune versions, so this walks
    rather than indexing a fixed path — a reader keyed on one layout goes silently
    empty when upstream reshapes it.
    """
    if isinstance(node, dict):
        if node.get("task_name") == task and "speedup" in node:
            return node
        for value in node.values():
            found = _find_result(value, task)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_result(value, task)
            if found is not None:
                return found
    return None


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
                    help=f"Per-instance reference-timing cache (default: {DEFAULT_TIMES_DIR}).")
    ap.add_argument("--no-baseline-cache", action="store_true",
                    help="Run the evaluator directly, re-measuring the whole reference pass on "
                         "EVERY call. Correct but slow — see run_evaluator.py.")
    ap.add_argument("--timeout", type=int, default=7200, help="Seconds to allow the evaluator.")
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

    dest_dir = root / "results" / args.model / args.task
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / "solver.py")

    summary = root / "reports" / "evaluate_summary.json"
    if summary.exists():
        summary.unlink()

    # The evaluator runs THROUGH `run_evaluator.py`, which gives `BaselineManager` a disk-backed
    # cache. Without it every node re-measures the whole reference pass in a fresh interpreter --
    # see that module's docstring for why the aggregate cache below does not cover this.
    if args.no_baseline_cache:
        argv = [sys.executable, str(evaluator)]
    else:
        argv = [sys.executable, str(RUNNER), str(args.baseline_times_dir), args.task, str(evaluator)]
    argv += ["--models", args.model, "--tasks", args.task]

    started = time.time()
    proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True, timeout=args.timeout)
    elapsed = round(time.time() - started, 1)

    out: dict[str, Any] = {"speedup": 0.0, "eval_seconds": elapsed}

    if not summary.exists():
        out["error"] = "evaluate_summary.json not produced"
        out["stderr_tail"] = proc.stderr[-1000:]
        print(json.dumps(out))
        return 0

    try:
        record = _find_result(json.loads(summary.read_text(encoding="utf-8")), args.task)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"unreadable summary: {type(exc).__name__}: {exc}"
        print(json.dumps(out))
        return 0

    if record is None:
        out["error"] = f"no record for task {args.task!r} in evaluate_summary.json"
        out["stderr_tail"] = proc.stderr[-1000:]
        print(json.dumps(out))
        return 0

    speedup = record.get("speedup")
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
            out["baseline_source"] = "unavailable"

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
