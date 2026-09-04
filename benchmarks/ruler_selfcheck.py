#!/usr/bin/env python3
"""The reference submitted as the candidate: what the ruler says about itself, today.

WHY. Point 5 of the standing sweep carries four numbers — `pagerank 1.0024, pde_heat1d 0.9958,
edge_expansion 0.9847, discrete_log 1.0162` — and `ruler_check.py` does not check them. It checks
that the cache is one regime with a full set of per-instance timings, which is the SHAPE of the
ruler. These are its READING: score the reference implementation itself and the answer must be ~1.0,
because `speedup = baseline_ms / optimized_ms` and both sides are then the same code.

They are not the same measurement in time, though, and that is the whole point. `baseline_ms` comes
out of the CACHE, written once (`edge_expansion` on 08-31 at 02:15); `optimized_ms` is timed NOW. So
the self-speedup is exactly the ratio of "how fast this box was when the cache was written" to "how
fast it is today", and a number that has walked away from 1.0 is drift in the ruler, not in any
solver.

MEASURED 2026-09-04, four repeats each, then three more of `edge_expansion` with the other lanes
idle to rule out load:

    edge_expansion   0.8849 0.8872 0.8994 0.8747  -> 0.8861   (sweep says 0.9847, -10.0 %)
    pde_heat1d       1.0346 1.0468 1.1045 1.0419  -> 1.0444   (sweep says 0.9958,  +4.9 %)
    discrete_log     1.0696 1.0767 1.0804 1.0711  -> 1.0739   (sweep says 1.0162,  +5.7 %)
    edge_expansion, solo:   0.8898 0.8810 0.8865  -> 0.8865   (load is not the cause)

Within a task the drift cancels — every probe of `edge_expansion` is divided by the same cached
baseline, so probe-vs-probe comparisons are untouched. What it does bite is any comparison ACROSS
TIME on one task: arm A's re-timed constants (§181) and arm B's corpus were measured months and
weeks apart on a ruler that has since moved ~10 % on the task both were measured on.

WHAT THIS DELIBERATELY DOES NOT DO is re-measure the cache. Re-timing the baseline would rescore
every future run against a different ruler than the 102 already in the corpus, and it would move the
ruler underneath a registered arm (§190). The drift is a number to carry, not a thing to erase.

TWO WAYS THIS REFUSES, both seen while building it and both worth recognising:
  * `speedup 0.0` with `eval_seconds` ~1.7 against a real ~28 s -- a HARNESS refusal, not a slow
    solver. The first attempt said `solver_unloadable`: `--solver-file-only` copies `solver.py` and
    nothing beside it, so the reference has to be INLINED, not imported. The second said
    `Task data directory not found` until `DATA_DIR` pointed at the HF dataset dir.
  * a cold cache reports the reference against itself at ~1.0 whatever was submitted (see
    `looplab_eval.py`'s `baseline_measured_in_pass`). A warm cache is what makes this a measurement.

Usage:
    ruler_selfcheck.py --task edge_expansion [--reps 4] [--lane 0-10,48-58] [--subset test]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

BENCH = "/var/tmp/looplab-bench"
HERE = Path(__file__).resolve().parent
# What the standing sweep says each task's self-speedup is. Kept beside the measurement so a drift
# is visible in one line instead of remembered.
SWEEP_SAYS = {"pagerank": 1.0024, "pde_heat1d": 0.9958,
              "edge_expansion": 0.9847, "discrete_log": 1.0162}


def build_solver(task: str, out_dir: str, probe_root: str = f"{BENCH}/model-probes") -> str:
    """Write a SELF-CONTAINED `solver.py` whose `solve()` is the reference's own.

    Inlined rather than imported: `--solver-file-only` copies one file, and an import of the
    reference module comes back `solver_unloadable` with `eval_seconds` 1.7.
    """
    found = sorted(glob.glob(f"{probe_root}/*/ws/{task}/reference_{task}.py"))
    if not found:
        raise FileNotFoundError(f"no delivered reference module for {task} under {probe_root}")
    body = Path(found[0]).read_text(encoding="utf-8")
    got = re.search(r"^class (\w+)\(Task\)", body, re.M)
    if not got:
        raise ValueError(f"{found[0]} has no `class X(Task)` to delegate to")
    cls = got.group(1)
    path = os.path.join(out_dir, "solver.py")
    Path(path).write_text(
        body + "\n\n"
        "class Solver:\n"
        '    """The reference itself, submitted as the candidate."""\n\n'
        f"    def __init__(self):\n        self._t = {cls}()\n\n"
        "    def solve(self, problem, **kwargs):\n        return self._t.solve(problem)\n",
        encoding="utf-8")
    return path


def one_eval(task: str, solver: str, lane: str, subset: str, timeout: float = 900.0) -> dict:
    env = dict(os.environ,
               DATA_DIR=f"{BENCH}/AlgoTune/.hf_datasets/oripress__AlgoTune/data",
               ALGOTUNE_BASELINE_CACHE_DIR=str(HERE / "algotune" / ".baseline_times"),
               ALGOTUNE_MIN_TIMEOUT_S="120", ALGOTUNE_EVAL_WORKERS="auto")
    argv = ["taskset", "-c", lane, sys.executable,
            str(HERE / "algotune" / "looplab_eval.py"),
            "--algotune-root", f"{BENCH}/AlgoTune", "--task", task,
            "--solver", solver, "--solver-file-only",
            "--baseline-times-dir", str(HERE / "algotune" / ".baseline_times"),
            "--subset", subset]
    got = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    try:
        return json.loads(got.stdout)
    except ValueError:
        return {"speedup": None, "eval_seconds": None,
                "no_speedup": {"reason": "unparseable", "stdout": got.stdout[-400:]}}


def refused(row: dict) -> str:
    """Why this reading is not a measurement, or "" if it is one."""
    if row.get("no_speedup"):
        return str((row["no_speedup"] or {}).get("reason") or "refused")
    seconds = row.get("eval_seconds")
    if row.get("speedup") in (0.0, None) and isinstance(seconds, (int, float)) and seconds < 5:
        # Point 2 of the sweep, in code: a zero that arrives in a second is the harness declining.
        return f"harness refusal (0.0 in {seconds}s)"
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", required=True)
    ap.add_argument("--reps", type=int, default=4)
    ap.add_argument("--lane", default="44-47,92-95")
    ap.add_argument("--subset", default="test", choices=("train", "test"))
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="ruler-selfcheck-") as tmp:
        solver = build_solver(args.task, tmp)
        vals, bad = [], []
        for _ in range(max(1, args.reps)):
            row = one_eval(args.task, solver, args.lane, args.subset)
            why = refused(row)
            if why:
                bad.append(why)
                continue
            vals.append(float(row["speedup"]))

    for why in bad:
        print(f"  REFUSED: {why}")
    if not vals:
        print(f"{args.task}: no measurement at all", file=sys.stderr)
        return 2
    median = statistics.median(vals)
    said = SWEEP_SAYS.get(args.task)
    line = (f"{args.task}: {[round(v, 4) for v in vals]} -> median {median:.4f}")
    if said is not None:
        line += f"; the sweep says {said:.4f} ({100 * (median - said) / said:+.1f} %)"
    print(line)
    if said is not None and abs(median - said) > 0.02:
        print("  DRIFT: the cached baseline and today's box no longer agree. Within one task this "
              "cancels (every probe is divided by the same cached baseline); across time on one "
              "task it does not, which is what arm A's re-timed constants are compared over.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
