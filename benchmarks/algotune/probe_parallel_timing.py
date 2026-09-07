#!/usr/bin/env python3
"""Does timing many instances AT ONCE change what the timer reads?

The arena's whole cost is the per-instance timing pass: one AlgoTuner task-arm spends ~97 % of its
wall clock there (measured: an arm-A run made 5 LLM calls in 40 minutes). The harness runs those
instances strictly one after another -- `evaluation_orchestrator.py:109`, `for i, problem_data in
enumerate(dataset)` -- and its only pool class, `BenchmarkPool`, is instantiated NOWHERE. So the
serial pass is not a decision anybody made; it is the absence of one.

This box has 96 cores under a 90-CPU quota. Before spending them, the question that actually decides
whether we may:

    a timing taken while 47 other timings are running -- is it the same number?

WHAT IT MEASURES

  1. **How many cores one instance really uses.** CPU-time / wall-time for a single unpinned solve.
     If a reference solver pulls 4 cores of threaded BLAS, then "one instance per core" is already
     oversubscription and the answer is decided before any pool exists.
  2. **Inflation vs concurrency.** The same instances timed at K = 1, 8, 24, 48 ... workers, each
     worker pinned to its own dedicated core(s). Reports the per-instance median at each level
     against the serial level.

HOW TO READ IT

The score AlgoTune reports is a RATIO -- `baseline_ms / solver_ms` -- so a slowdown that hits both
halves equally cancels. What does NOT cancel is (a) inflation that varies between the baseline pass
and the solver pass, and (b) variance: a noisier timer makes a real speedup harder to see. So the
number to watch is not "is there inflation" but "is it uniform and is the spread still tight".

    python probe_parallel_timing.py --task convex_hull --instances 48 --levels 1,8,24,48
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import statistics
import sys
import time


def _worker(payload):
    """Time a slice of instances on a dedicated core set. Runs in a forked child."""
    core_list, items, repeats, task_name, quiet = payload
    if core_list:
        os.sched_setaffinity(0, set(core_list))
    if quiet:
        import logging
        logging.disable(logging.CRITICAL)

    from AlgoTuneTasks.factory import TaskFactory

    task = TaskFactory(task_name)
    out = []
    for idx, problem, warmup in items:
        # Same shape as the harness: warm the code paths on a DIFFERENT problem so the answer is
        # not cached, then time the real one. `min` of N runs, as AlgoTune scores by.
        try:
            task.solve(warmup)
        except Exception:
            pass
        times = []
        cpu0 = time.process_time()
        wall0 = time.perf_counter()
        for _ in range(repeats):
            t0 = time.perf_counter()
            try:
                task.solve(problem)
            except Exception as exc:  # a solver that throws is data, not a crash
                out.append({"idx": idx, "error": f"{type(exc).__name__}: {exc}"[:120]})
                times = []
                break
            times.append((time.perf_counter() - t0) * 1000.0)
        if not times:
            continue
        wall = time.perf_counter() - wall0
        cpu = time.process_time() - cpu0
        out.append({
            "idx": idx,
            "min_ms": min(times),
            "median_ms": statistics.median(times),
            "cores_used": round(cpu / wall, 2) if wall > 0 else None,
        })
    return out


def run_level(task_name, instances, k, cores_per_worker, repeats, quiet):
    """Time every instance with k workers running concurrently, each on its own cores."""
    slices = [[] for _ in range(k)]
    for n, item in enumerate(instances):
        slices[n % k].append(item)

    payloads = []
    for w in range(k):
        lo = w * cores_per_worker
        cores = list(range(lo, lo + cores_per_worker))
        payloads.append((cores, slices[w], repeats, task_name, quiet))

    ctx = mp.get_context("fork")
    t0 = time.perf_counter()
    with ctx.Pool(processes=k) as pool:
        chunks = pool.map(_worker, payloads)
    wall = time.perf_counter() - t0
    rows = [r for chunk in chunks for r in chunk]
    return rows, wall


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", required=True)
    ap.add_argument("--instances", type=int, default=48)
    ap.add_argument("--levels", default="1,8,24,48")
    ap.add_argument("--cores-per-worker", type=int, default=1)
    ap.add_argument("--repeats", type=int, default=3, help="timed runs per instance (harness: 3)")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--verbose", action="store_true", help="leave AlgoTuner's logging on")
    args = ap.parse_args()

    quiet = not args.verbose
    if quiet:
        import logging
        logging.disable(logging.CRITICAL)

    from AlgoTuneTasks.factory import TaskFactory

    task = TaskFactory(args.task)
    # Default sizes ONLY. Passing custom train/test sizes makes `load_dataset` REGENERATE the
    # dataset instead of using the cached one the scorer itself loads -- measured: 13 minutes of
    # solid CPU on convex_hull before it was killed, against seconds for the cached path. Slice
    # the instances afterwards instead.
    train, _test = task.load_dataset()
    problems = []
    for rec in train:
        problems.append(rec["problem"] if isinstance(rec, dict) and "problem" in rec else rec)
        if len(problems) >= args.instances:
            break
    if len(problems) < 2:
        print(f"only {len(problems)} instances loaded; need at least 2", file=sys.stderr)
        return 2

    instances = [(i, p, problems[(i + 1) % len(problems)]) for i, p in enumerate(problems)]
    print(f"task {args.task}: {len(instances)} instances, {args.repeats} timed runs each, "
          f"{args.cores_per_worker} core(s) per worker", flush=True)

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    quota = len(os.sched_getaffinity(0))
    results = {}
    for k in levels:
        if k * args.cores_per_worker > quota:
            print(f"  level {k}: SKIPPED -- {k}x{args.cores_per_worker} cores exceeds the "
                  f"{quota} this process may use", flush=True)
            continue
        rows, wall = run_level(args.task, instances, k, args.cores_per_worker, args.repeats, quiet)
        ok = [r for r in rows if "min_ms" in r]
        errs = [r for r in rows if "error" in r]
        results[k] = {"rows": {r["idx"]: r["min_ms"] for r in ok}, "wall": wall,
                      "errors": len(errs),
                      "cores_used": statistics.median([r["cores_used"] for r in ok
                                                       if r.get("cores_used")]) if ok else None}
        print(f"  level {k:>3}: {len(ok)} timed, {len(errs)} errors, wall {wall:6.1f}s, "
              f"median per-instance {statistics.median([r['min_ms'] for r in ok]):8.2f} ms"
              if ok else f"  level {k}: no timings", flush=True)
        if errs:
            print(f"        first error: {errs[0]['error']}", flush=True)

    base = results.get(levels[0])
    if base:
        print(f"\n{'level':>6}{'wall s':>9}{'median ms':>12}{'vs serial':>11}"
              f"{'p90 inflation':>15}{'cores/inst':>12}")
        print("-" * 65)
        for k, res in results.items():
            shared = [i for i in res["rows"] if i in base["rows"]]
            ratios = sorted(res["rows"][i] / base["rows"][i] for i in shared
                            if base["rows"][i] > 0)
            med = statistics.median(res["rows"].values()) if res["rows"] else float("nan")
            p90 = ratios[int(len(ratios) * 0.9)] if ratios else float("nan")
            print(f"{k:>6}{res['wall']:>9.1f}{med:>12.2f}"
                  f"{(statistics.median(ratios) if ratios else float('nan')):>10.2f}x"
                  f"{p90:>14.2f}x{str(res['cores_used']):>12}")
        print("\nRATIO CANCELS UNIFORM SLOWDOWN: both halves of speedup = baseline/solver are timed "
              "in the same regime.\nWhat a large p90 means is the timer got NOISY, and noise does "
              "not cancel.")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"task": args.task, "cores_per_worker": args.cores_per_worker,
                       "levels": {str(k): {"wall": v["wall"], "errors": v["errors"],
                                           "cores_used": v["cores_used"],
                                           "median_ms": statistics.median(v["rows"].values())
                                           if v["rows"] else None}
                                  for k, v in results.items()}}, fh, indent=1)
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
