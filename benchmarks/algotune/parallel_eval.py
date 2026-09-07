# SOURCE OF TRUTH, AND IT HAD DRIFTED. `patch_parallel_eval.py` copies this file into the arena as
# `AlgoTuner/utils/evaluator/looplab_parallel.py`, so this is what a rebuilt stand installs. On
# 2026-09-01 the two had diverged by 49 lines: `apply_thread_budget` existed ONLY in the deployed
# copy, and `_pool_init`, `prefetch_oracle` and `prefetch_results` differed. `resolve_workers` --
# which decides the baseline cache key -- was identical, so the ruler's NAME was never at risk; what
# was at risk is its VALUE. The missing function holds BLAS/OpenMP threads down to a worker's core
# count, and its own docstring measures what happens without it: 893.5 ms serial, 5708.6 ms through
# the pinned pool, 872.2 ms with the budget applied. Rebuilding a stand from the stale copy would
# have restored a 6.5x oversubscription distortion under the same regime key, and nothing checked.
#
# The deployed file is the authority here for the same reason it was for `baseline_manager.py`: it
# is what every number in this corpus was measured with. `tests/test_parallel_eval_matches_the_arena
# .py` pins the two together so the next drift is a red test rather than a silent instrument change.
"""Evaluate a task's instances CONCURRENTLY, each on its own core.

Copied into the AlgoTune checkout by `patch_parallel_eval.py` as
`AlgoTuner/utils/evaluator/looplab_parallel.py`. It is the only file of ours that runs inside the
third-party harness, and it is deliberately tiny.

WHY
---
`evaluation_orchestrator.py` walks a task's ~100 instances one at a time, and each instance forks a
fresh process per timed run. Measured on the reference box, the timed solver calls are 0.1-0.9 s
inside ~16 s per instance -- i.e. ~98 % of the arena's wall clock is process spawn, done serially,
on a machine with 90 usable cores. The serial walk is not a decision anybody made: the harness's own
pool class, `BenchmarkPool`, is defined and instantiated NOWHERE.

WHY IT DOES NOT CHANGE WHAT IS MEASURED
--------------------------------------
Measured 2026-08-20 on this box, timing the same instances at 1 / 8 / 24 / 48 concurrent workers,
each pinned to its own core:

    task                 serial wall   48-way wall   median inflation   p90    cores/instance
    discrete_log              4.3 s        1.0 s         1.04x         1.18x        1.0
    convex_hull               6.1 s        0.9 s         0.99x         1.01x        1.0
    spectral_clustering     106.7 s        5.8 s         1.01x         1.09x        1.0

Two things that had to be true, and are:

  * **One instance uses one core.** `cores_used` (process CPU time / wall time) is 1.0 on all three
    -- no threaded BLAS quietly claiming four cores, so one-instance-per-core is not
    oversubscription.
  * **The timer reads the same number.** The score is a RATIO, `baseline_ms / solver_ms`, and both
    halves run through this same path in the same regime, so even the 1-4 % that does appear
    cancels. What would not cancel is spread, and the p90 stays within a tenth.

CONSTRAINTS THIS FILE KEEPS
---------------------------
1. **Workers partition the INHERITED affinity mask** (`os.sched_getaffinity`). A campaign lane is
   pinned with `taskset`; a worker pool that picked core numbers of its own would walk straight out
   of its lane and onto cores another lane is timing on. Never widen, only divide.
2. **The warmup problem stays the NEXT problem in the dataset**, exactly as the serial loop chooses
   it. Warmup deliberately runs a different problem so the code path is warm and the answer is not;
   changing it would turn a memoising solver into an unbounded speedup.
3. **A failure here is not a failure of the run.** Anything this file cannot produce is simply
   absent from the map it returns, and the caller's untouched serial loop computes it the old way.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os


def _plan_cores(workers: int, cores_per_worker: int) -> list[list[int]]:
    """Split the cores THIS process is already allowed to use into one set per worker."""
    allowed = sorted(os.sched_getaffinity(0))
    plan = []
    for w in range(workers):
        lo = (w * cores_per_worker) % max(len(allowed), 1)
        chunk = [allowed[(lo + k) % len(allowed)] for k in range(cores_per_worker)]
        plan.append(sorted(set(chunk)))
    return plan


def resolve_workers(env: dict | None = None) -> tuple[int, int]:
    """(workers, cores_per_worker) from the environment. Default (1, 1) = upstream behaviour."""
    env = env or os.environ
    cores_per_worker = max(1, int(env.get("ALGOTUNE_EVAL_CORES_PER_WORKER", "1") or 1))
    raw = (env.get("ALGOTUNE_EVAL_WORKERS", "1") or "1").strip().lower()
    allowed = len(os.sched_getaffinity(0))
    if raw in ("auto", "max"):
        return max(1, allowed // cores_per_worker), cores_per_worker
    try:
        workers = int(raw)
    except ValueError:
        return 1, cores_per_worker
    if workers <= 1:
        return 1, cores_per_worker
    # Never hand out more workers than there are cores to pin them to: two workers sharing a core
    # would time each other's contention and nothing in the output would say so.
    return max(1, min(workers, allowed // cores_per_worker)), cores_per_worker


# Set in the parent before the pool forks, read by the workers through inherited memory. They are
# module globals rather than closure variables on purpose: `multiprocessing.Pool` PICKLES the
# initializer and the mapped function even under the fork start method, and a closure is not
# picklable -- the first version was, which made the pool raise, the caller catch, and every run
# fall silently back to serial while reporting nothing. The evidence that it was happening at all
# was the missing breadcrumb.
_CTX: dict = {}


def apply_thread_budget(n_cores):
    """Hold the BLAS/OpenMP thread count down to the number of cores this process may use.

    Pinning a worker to one core does NOT reduce the thread count the runtime chose: libgomp fixed
    it from the CPU count it saw when numpy was imported, in the unpinned parent. Every timed child
    then runs that many threads on one core and spin-waits. Measured on this box, 24 rbf instances:
    893.5 ms of measured time serial, 5708.6 ms through the pinned pool, 872.2 ms through the pinned
    pool with this budget applied -- the serial number, at the pool's speed.

    `os.environ` alone cannot do it, which is why upstream's own
    `os.environ.setdefault("OMP_NUM_THREADS", "1")` in run_isolated_benchmark has no effect: the
    variable was read at import. threadpoolctl sets it through each runtime's API, and the setting
    survives the fork into the timed child. The env vars are set too, for any child that initialises
    a runtime from scratch rather than inheriting ours.
    """
    n = str(max(1, int(n_cores)))
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[var] = n
    try:
        import threadpoolctl

        threadpoolctl.threadpool_limits(int(n))
    except Exception as exc:  # noqa: BLE001 - a missing limiter must not fail the run
        logging.getLogger(__name__).warning(
            "looplab_parallel: thread budget not enforced in-process (%r); timings will be "
            "inflated by oversubscription", exc)


def _pool_init():
    """Claim one core set per worker, once. Every timed child forked later inherits the pin."""
    counter = _CTX["counter"]
    plan = _CTX["core_plan"]
    with counter.get_lock():
        slot = counter.value % len(plan)
        counter.value += 1
    try:
        os.sched_setaffinity(0, set(plan[slot]))
    except OSError as exc:
        logging.getLogger(__name__).warning("looplab_parallel: cannot pin worker: %s", exc)
    # The pin decides how many cores this worker has; the thread budget has to agree with it, or
    # the worker spends its one core arbitrating between threads that cannot run.
    apply_thread_budget(len(plan[slot]))


def _pool_run(item):
    i, problem, warmup_problem, problem_id, baseline_time_ms, metadata = item
    try:
        return i, _CTX["orchestrator"].evaluate_single(
            task_instance=_CTX["task_instance"],
            problem=problem,
            solver_func=_CTX["solver_func"],
            warmup_problem=warmup_problem,
            problem_id=problem_id,
            problem_index=i,
            baseline_time_ms=baseline_time_ms,
            problem_metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001 - one instance failing is not the run failing
        logging.getLogger(__name__).warning(
            "looplab_parallel: instance %s failed in worker (%r); the serial loop recomputes it",
            i, exc)
        return i, None


def prefetch_results(orchestrator, dataset, task_instance, solver_func, baseline_times, task_name,
                     workers, cores_per_worker):
    """Evaluate every instance concurrently; return {index: ProblemResult}.

    PROCESSES, not threads, and that was measured rather than assumed. The first version used a
    thread pool, reasoning that `evaluate_single` mostly waits on the child the harness forks per
    timed run and that a waiting thread holds no GIL. Measured on this box: 100 instances of
    `discrete_log` through a 24-THREAD pool took 109.6 s against ~130 s serial -- a 1.2x that is not
    parallelism. Enough of `evaluate_single` runs in-process to hold the GIL, and threads cannot
    overlap that.

    A forked pool has no GIL to share. It costs one constraint: what comes back must pickle. If a
    result does not, that instance is absent from the map and the caller's untouched serial loop
    computes it the old way.
    """
    log = logging.getLogger(__name__)
    items = []
    for i, problem_data in enumerate(dataset):
        problem, metadata = orchestrator._extract_problem_data(problem_data)
        problem_id = metadata.get("id", f"problem_{i + 1}")
        if problem_id is not None:
            problem_id = str(problem_id)
        warmup_problem, _ = orchestrator._extract_problem_data(dataset[(i + 1) % len(dataset)])
        metadata["task_name"] = task_name
        baseline_time_ms = (baseline_times.get(problem_id) if baseline_times
                            else metadata.get("baseline_time_ms"))
        items.append((i, problem, warmup_problem, problem_id, baseline_time_ms, metadata))

    ctx = mp.get_context("fork")
    _CTX.update({
        "orchestrator": orchestrator,
        "task_instance": task_instance,
        "solver_func": solver_func,
        "core_plan": _plan_cores(workers, cores_per_worker),
        "counter": ctx.Value("i", 0),
    })

    log.info("looplab_parallel: evaluating %d instances on %d workers x %d core(s) out of %d "
             "allowed", len(items), workers, cores_per_worker, len(os.sched_getaffinity(0)))
    # A breadcrumb, because the harness reconfigures logging and our INFO line does not survive it.
    # Without it there is no telling "the pool ran and did not help" from "the pool never ran", and
    # those have opposite fixes.
    import json as _json
    import time as _time

    trace = os.environ.get("ALGOTUNE_PARALLEL_TRACE")
    t0 = _time.time()
    out = {}
    from concurrent.futures import ProcessPoolExecutor, as_completed

    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                             initializer=_pool_init) as pool:
        futures = [pool.submit(_pool_run, it) for it in items]
        for fut in as_completed(futures):
            i, result = fut.result()
            if result is not None:
                out[i] = result
    if trace:
        try:
            with open(trace, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps({"task": task_name, "instances": len(items),
                                      "returned": len(out), "workers": workers,
                                      "cores_per_worker": cores_per_worker,
                                      "wall_s": round(_time.time() - t0, 2), "pid": os.getpid()})
                         + "\n")
        except OSError:
            pass
    return out


def _oracle_run(payload):
    """One reference (oracle) timing, in a pool worker."""
    problem_id, kwargs = payload
    from AlgoTuner.utils.isolated_benchmark import run_isolated_benchmark

    try:
        return problem_id, run_isolated_benchmark(**kwargs)
    except Exception as exc:  # noqa: BLE001
        # The reason travels back with the result. A worker's log line does not survive the
        # harness's logging config, and "returned 0 of 100" with no reason is a debugging dead end
        # -- measured: the first version of this pool failed every job and looked like a slow one.
        logging.getLogger(__name__).warning(
            "looplab_parallel: oracle for %s failed in worker (%r)", problem_id, exc)
        return problem_id, {"__looplab_error__": f"{type(exc).__name__}: {exc}"[:300]}


def prefetch_oracle(jobs, workers, cores_per_worker):
    """Pre-measure every instance's REFERENCE time, in the same regime the solver will be timed in.

    This is not an optimisation, it is the thing that makes the optimisation legitimate. The score
    is `baseline_ms / solver_ms`; measure the numerator alone on an idle box and the denominator
    while 24 instances run, and the ratio stops meaning anything. Measured on this box with the
    solver pass parallel and the baseline pass still serial, the identical shipped solver scored
    1.09x, 1.43x and 1.78x depending only on which halves were contended.

    `jobs` is [(problem_id, kwargs_for_run_isolated_benchmark)]. Returns {problem_id: result}.
    """
    log = logging.getLogger(__name__)
    ctx = mp.get_context("fork")
    _CTX["core_plan"] = _plan_cores(workers, cores_per_worker)
    _CTX["counter"] = ctx.Value("i", 0)
    log.info("looplab_parallel: oracle pass over %d instances on %d workers", len(jobs), workers)

    import json as _json
    import time as _time

    t0 = _time.time()
    out = {}
    errors = []
    from concurrent.futures import ProcessPoolExecutor, as_completed

    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                             initializer=_pool_init) as pool:
        for _fut in as_completed([pool.submit(_oracle_run, j) for j in jobs]):
            problem_id, result = _fut.result()
            if isinstance(result, dict) and "__looplab_error__" in result:
                errors.append(result["__looplab_error__"])
            elif result is not None:
                out[problem_id] = result
    if errors:
        log.warning("looplab_parallel: %d/%d oracle jobs failed; first: %s",
                    len(errors), len(jobs), errors[0])
    trace = os.environ.get("ALGOTUNE_PARALLEL_TRACE")
    if trace:
        try:
            with open(trace, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps({"phase": "oracle", "instances": len(jobs),
                                      "returned": len(out), "failed": len(errors),
                                      "first_error": errors[0] if errors else None,
                                      "workers": workers,
                                      "wall_s": round(_time.time() - t0, 2)}) + "\n")
        except OSError:
            pass
    return out
