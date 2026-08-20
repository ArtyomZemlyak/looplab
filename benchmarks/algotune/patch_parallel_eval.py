#!/usr/bin/env python3
"""Let a task's instances be timed concurrently, one per core, instead of one after another.

Patches `AlgoTuner/utils/evaluator/evaluation_orchestrator.py` at exactly TWO points and copies
`parallel_eval.py` in beside it as `looplab_parallel.py`:

  1. **Before the loop** -- if `ALGOTUNE_EVAL_WORKERS` > 1, evaluate every instance concurrently
     into a `{index: result}` map.
  2. **At `result = self.evaluate_single(...)`** -- take the pre-computed result when there is one.

The serial loop itself is left ALONE: the logging, the critical-error branch, the consecutive-failure
abort, the progress callback and the aggregation all run exactly as upstream wrote them, over exactly
the same results. That is the point of prefetching rather than rewriting -- the smallest possible
change to a third-party file whose output is a published number.

Two consequences worth stating plainly:

  * With a prefetch, the consecutive-failure abort no longer SAVES work; it still produces the same
    truncated result, but every instance has already been evaluated. On this box that trade is
    strongly positive: the pathological case docs/48 records -- one timing-out candidate costing
    100 x `baseline_timeout` -- is precisely the case that parallelises best.
  * `ALGOTUNE_EVAL_WORKERS` unset or 1 leaves upstream behaviour bit-for-bit. The patch is inert
    until a campaign asks for it, and it applies to BOTH arms, since both evaluate through this
    harness.

Idempotent, keeps a `.orig`, `--revert` undoes it.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys

MARK = "LOOPLAB PARALLEL EVAL"

PREFETCH_ANCHOR = """        results = []

        # Track consecutive failures for early abort
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "5"))
"""

PREFETCH_BLOCK = '''        results = []

        # --- {mark} (patched; see benchmarks/algotune/patch_parallel_eval.py) ---
        # Every instance is independent, and each already forks its own process per timed run.
        # With ALGOTUNE_EVAL_WORKERS > 1 they are evaluated concurrently, each worker pinned to its
        # own core out of the mask this process already holds. Unset or 1 -> upstream behaviour.
        _looplab_prefetch = {{}}
        try:
            from AlgoTuner.utils.evaluator import looplab_parallel as _ll_par

            _ll_workers, _ll_cores = _ll_par.resolve_workers()
            if _ll_workers > 1 and len(dataset) > 1:
                _looplab_prefetch = _ll_par.prefetch_results(
                    self, dataset, task_instance, solver_func, baseline_times, task_name,
                    _ll_workers, _ll_cores,
                )
        except Exception as _ll_exc:  # a parallel failure must not be a run failure
            self.logger.warning(f"{mark}: falling back to serial ({{_ll_exc!r}})")
            _looplab_prefetch = {{}}
        # --- end {mark} ---

        # Track consecutive failures for early abort
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "5"))
'''.format(mark=MARK)

CALL_ANCHOR = """            # Evaluate single problem
            result = self.evaluate_single(
"""

CALL_BLOCK = """            # Evaluate single problem
            result = _looplab_prefetch.get(i)          # {mark}
            if result is None:
                result = self.evaluate_single(
""".format(mark=MARK)

# The original call spans several keyword lines and closes with `            )`. Once the call is
# nested one level deeper, those lines have to move with it -- done textually below rather than by
# guessing an indentation rule.
CALL_TAIL_ANCHOR = """                task_instance=task_instance,
                problem=problem,
                solver_func=solver_func,
                warmup_problem=warmup_problem,
                problem_id=problem_id,
                problem_index=i,
                baseline_time_ms=baseline_time_ms,
                problem_metadata=metadata,
            )
"""

CALL_TAIL_BLOCK = """                    task_instance=task_instance,
                    problem=problem,
                    solver_func=solver_func,
                    warmup_problem=warmup_problem,
                    problem_id=problem_id,
                    problem_index=i,
                    baseline_time_ms=baseline_time_ms,
                    problem_metadata=metadata,
                )
"""


# --- second file: the per-run timeout FLOOR -----------------------------------------------------
# `solver_executor.py` bounds each timed subprocess at `max(10 x baseline, FLOOR)`, and upstream's
# own comment says the floor is there "to account for import overhead" -- it is a startup allowance,
# not a claim about the solver. Under concurrency startup costs more (48 children importing at once),
# so a floor sized for a serial run turns into spurious timeouts -- and ONE timeout makes the whole
# task score N/A, since 100 % instance validity is required for any speedup at all.
#
# Measured on this box, `discrete_log` scored by the shipped GPT-5.4 solver:
#   serial          132 s   speedup 1.4611   0 timeouts
#   8 workers       127 s   N/A              1 timeout
#   16 workers      101 s   N/A             23 timeouts
#   24 workers       58 s   N/A             94 timeouts
#
# The floor becomes env-tunable; the DEFAULT is upstream's own value, so an unset environment is
# upstream behaviour exactly. Raising it changes no recorded timing -- only when a run is declared
# timed out -- and it applies to both arms, which evaluate through this same harness.
FLOOR_HELPER = '''
def _looplab_min_timeout(default_s):
    """Per-subprocess timeout FLOOR, env-tunable (LOOPLAB PARALLEL EVAL).

    The floor is an allowance for process startup, not a bound on the solver: upstream sets it
    precisely because a fast baseline would otherwise convert import time into a false timeout.
    Concurrency makes startup more expensive, so the allowance has to be able to grow with it.
    """
    import os as _os

    try:
        return max(float(default_s), float(_os.environ.get("ALGOTUNE_MIN_TIMEOUT_S", default_s)))
    except (TypeError, ValueError):
        return float(default_s)

'''

FLOOR_SITES = (
    ("""            timeout_seconds = max(
                timeout_seconds, 5.0
            )  # At least 5 seconds to account for import overhead""",
     """            timeout_seconds = max(
                timeout_seconds, _looplab_min_timeout(5.0)
            )  # At least 5 seconds to account for import overhead"""),
    ("""            timeout_seconds = max(
                timeout_seconds, 10.0
            )  # At least 10 seconds to account for import overhead in SLURM""",
     """            timeout_seconds = max(
                timeout_seconds, _looplab_min_timeout(10.0)
            )  # At least 10 seconds to account for import overhead in SLURM"""),
)


def patch_floor(root, revert=False):
    target = root / "AlgoTuner/utils/evaluator/solver_executor.py"
    backup = target.with_suffix(target.suffix + ".orig")
    if revert:
        if backup.exists():
            shutil.copy2(backup, target)
            print(f"restored {target.name} from {backup.name}")
        return 0
    text = target.read_text(encoding="utf-8")
    if "_looplab_min_timeout" in text:
        print(f"already patched: {target.name}")
        return 0
    if not backup.exists():
        shutil.copy2(target, backup)
    for old, new in FLOOR_SITES:
        if old not in text:
            print(f"FAILED: floor site not found in {target.name}", file=sys.stderr)
            return 1
        text = text.replace(old, new, 1)
    # Define the helper just before the first class in the file.
    marker = "\nclass "
    idx = text.index(marker)
    text = text[:idx] + "\n" + FLOOR_HELPER + text[idx:]
    import ast
    try:
        ast.parse(text)
    except SyntaxError as exc:
        print(f"FAILED: patched {target.name} does not parse ({exc})", file=sys.stderr)
        return 1
    target.write_text(text, encoding="utf-8")
    print(f"patched {target}\n  backup: {backup.name}\n  set ALGOTUNE_MIN_TIMEOUT_S to raise the "
          f"startup allowance under concurrency")
    return 0



# --- third file: the ORACLE pass ------------------------------------------------------------------
# `baseline_manager.py::_generate_baseline_times` walks the same instances to measure the REFERENCE
# time, in its own serial loop. Parallelising only the solver pass would leave the two halves of
# `baseline_ms / solver_ms` measured in different regimes -- observed on this box as the same shipped
# solver scoring 1.09x, 1.43x and 1.78x depending purely on which half was contended. So the oracle
# pass gets the same pool, and the baseline cache key carries the regime (patch_baseline_cache.py).
ORACLE_ANCHOR = """        problem_count = len(dataset_list)
        for i, item in enumerate(dataset_list):
"""

ORACLE_BLOCK = """        problem_count = len(dataset_list)

        # --- {mark} (patched) ---
        # Pre-measure every reference timing concurrently, in the SAME regime the solver pass will
        # be timed in. Falls back to the untouched serial path for anything it does not return.
        _ll_oracle = {{}}
        try:
            from AlgoTuner.utils.evaluator import looplab_parallel as _ll_par

            _ll_w, _ll_c = _ll_par.resolve_workers()
            if use_isolated and _ll_w > 1 and problem_count > 1:
                _ll_task_name = getattr(self.task_instance, "task_name", "unknown_task")
                _ll_code_dir = self.task_instance.get_task_directory()
                _ll_timeout = 60.0
                if getattr(self.task_instance, "target_time_ms", None):
                    _ll_timeout = max(60.0, (self.task_instance.target_time_ms / 1000.0) * 10.0)
                _ll_jobs = []
                for _ll_i, _ll_item in enumerate(dataset_list):
                    if isinstance(_ll_item, dict):
                        _ll_id = _ll_item.get("id", _ll_item.get("seed", _ll_item.get("k", None)))
                        _ll_problem = _ll_item.get("problem", _ll_item)
                    else:
                        _ll_id, _ll_problem = None, _ll_item
                    if _ll_id is None:
                        _ll_id = f"problem_{{_ll_i + 1}}"
                    _ll_warm_item = dataset_list[(_ll_i + 1) % problem_count]
                    _ll_warm = (_ll_warm_item.get("problem", _ll_warm_item)
                                if isinstance(_ll_warm_item, dict) else _ll_warm_item)
                    _ll_jobs.append((str(_ll_id), dict(
                        task_name=_ll_task_name, code_dir=_ll_code_dir,
                        warmup_problem=_ll_warm, timed_problem=_ll_problem,
                        num_runs=num_runs, timeout_seconds=_ll_timeout,
                    )))
                _ll_oracle = _ll_par.prefetch_oracle(_ll_jobs, _ll_w, _ll_c)
        except Exception as _ll_exc:            # a parallel failure must not be a run failure
            logging.warning("{mark}: oracle pass falling back to serial (%r)", _ll_exc)
            _ll_oracle = {{}}
        # --- end {mark} ---

        for i, item in enumerate(dataset_list):
""".format(mark=MARK)

ORACLE_CALL_ANCHOR = """                        benchmark_result = run_isolated_benchmark(
                            task_name=task_name,
                            code_dir=code_dir,
                            warmup_problem=warmup_problem_data,
                            timed_problem=problem_data,
                            num_runs=num_runs,
                            timeout_seconds=timeout_seconds,
                        )
"""

ORACLE_CALL_BLOCK = """                        benchmark_result = _ll_oracle.pop(problem_id, None)   # {mark}
                        if benchmark_result is None:
                            benchmark_result = run_isolated_benchmark(
                                task_name=task_name,
                                code_dir=code_dir,
                                warmup_problem=warmup_problem_data,
                                timed_problem=problem_data,
                                num_runs=num_runs,
                                timeout_seconds=timeout_seconds,
                            )
""".format(mark=MARK)


def patch_oracle(root, revert=False):
    target = root / "AlgoTuner/utils/evaluator/baseline_manager.py"
    backup = target.with_suffix(target.suffix + ".parallel.orig")
    if revert:
        if backup.exists():
            shutil.copy2(backup, target)
            print(f"restored {target.name} from {backup.name}")
        return 0
    text = target.read_text(encoding="utf-8")
    if MARK in text:
        print(f"already patched: {target.name} (oracle pass)")
        return 0
    for anchor in (ORACLE_ANCHOR, ORACLE_CALL_ANCHOR):
        if anchor not in text:
            print(f"FAILED: oracle anchor not found in {target.name}", file=sys.stderr)
            return 1
    if not backup.exists():
        shutil.copy2(target, backup)
    text = text.replace(ORACLE_ANCHOR, ORACLE_BLOCK, 1)
    text = text.replace(ORACLE_CALL_ANCHOR, ORACLE_CALL_BLOCK, 1)
    import ast
    try:
        ast.parse(text)
    except SyntaxError as exc:
        print(f"FAILED: patched {target.name} does not parse ({exc})", file=sys.stderr)
        return 1
    target.write_text(text, encoding="utf-8")
    print(f"patched {target}\n  backup: {backup.name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--algotune-root", required=True)
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    root = pathlib.Path(args.algotune_root)
    target = root / "AlgoTuner/utils/evaluator/evaluation_orchestrator.py"
    helper_dst = root / "AlgoTuner/utils/evaluator/looplab_parallel.py"
    helper_src = pathlib.Path(__file__).with_name("parallel_eval.py")
    backup = target.with_suffix(target.suffix + ".orig")

    if not target.exists():
        print(f"not an AlgoTune checkout: {target} missing", file=sys.stderr)
        return 2

    if args.revert:
        if backup.exists():
            shutil.copy2(backup, target)
            print(f"restored {target.name} from {backup.name}")
        helper_dst.unlink(missing_ok=True)
        patch_floor(root, revert=True)
        patch_oracle(root, revert=True)
        return 0

    # The helper is refreshed on every run: it lives in this repo, and a stale copy inside a
    # third-party checkout is the kind of drift nobody notices until a number is wrong.
    shutil.copy2(helper_src, helper_dst)

    rc = patch_floor(root)
    if rc:
        return rc
    # The ORACLE pass patch is NOT applied by default. Measured 2026-08-20: it fails 100/100 jobs
    # with `AttributeError: Class 'Solver' not found in solver module` -- `run_isolated_benchmark`
    # loads a solver module out of `code_dir`, and for the reference pass that directory is the TASK
    # package, which has no `Solver` class. The serial path reaches its number some other way, and
    # until that way is identified, a prefetch that always fails and silently falls back is the
    # silent-empty-answer shape this repo keeps paying for. Set ALGOTUNE_PATCH_ORACLE=1 to apply it
    # anyway (it is harmless -- it falls back -- but it buys nothing).
    if os.environ.get("ALGOTUNE_PATCH_ORACLE") == "1":
        rc = patch_oracle(root)
        if rc:
            return rc

    text = target.read_text(encoding="utf-8")
    if MARK in text:
        print(f"already patched: {target.name} (helper refreshed)")
        return 0
    if not backup.exists():
        shutil.copy2(target, backup)

    for anchor in (PREFETCH_ANCHOR, CALL_ANCHOR, CALL_TAIL_ANCHOR):
        if anchor not in text:
            print("FAILED: upstream shape changed; patch not applied\n"
                  f"missing anchor:\n{anchor}", file=sys.stderr)
            return 1

    text = text.replace(PREFETCH_ANCHOR, PREFETCH_BLOCK, 1)
    text = text.replace(CALL_ANCHOR, CALL_BLOCK, 1)
    text = text.replace(CALL_TAIL_ANCHOR, CALL_TAIL_BLOCK, 1)

    import ast
    try:
        ast.parse(text)
    except SyntaxError as exc:
        print(f"FAILED: patched file does not parse ({exc}); nothing written", file=sys.stderr)
        return 1

    target.write_text(text, encoding="utf-8")
    print(f"patched {target}")
    print(f"  helper: {helper_dst.name}")
    print(f"  backup: {backup.name}")
    print("  set ALGOTUNE_EVAL_WORKERS=<n|auto> (and optionally ALGOTUNE_EVAL_CORES_PER_WORKER)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
