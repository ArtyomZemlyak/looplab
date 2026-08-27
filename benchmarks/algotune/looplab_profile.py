#!/usr/bin/env python3
"""Line-by-line profile of a candidate `solver.py` on ONE REAL train instance.

WHY THIS FILE EXISTS
--------------------
The reference agent has two commands this arm had none of: ``profile <file> <input>`` and
``profile_lines <file> <lines> <input>`` (its system prompt, e.g.
``campaign-final/A-convex_hull.log:181``), implemented by ``AlgoTuner/utils/profiler.py`` --
``from line_profiler import LineProfiler`` at line 8, ``self.line_profiler(solve_method)`` at
line 122 -- and their output is the ``Line # / Hits / Time / Per Hit / % Time`` table that arm A
reads dozens of times per run (a live one at ``campaign-final/A-discrete_log.log:36119``).
Measured 2026-08-27: ``AlgoTune/.venv/bin/python -c "import line_profiler; print(...__version__)"``
-> ``5.0.2``, so the capability was installed in the very venv our scores are computed in and only
our arm could not reach it. Without it the Developer can see THAT its solver is slow (``eval_train``
prints one speedup) and never WHERE.

WHY IT IS A SEPARATE, ARGUMENT-FREE COMMAND
-------------------------------------------
Our developer commands are operator-pinned: ``looplab/tools/dev_commands.py:143`` tells the model
"You select only its name; argv, cwd, env and timeout cannot be changed". ``profile <input>`` is
therefore inexpressible here. What IS expressible is a command that picks the instance itself, and
that is what this script is: instance 0 of the TRAIN split -- the same split ``looplab_eval.py``
scores nodes on, taken from the same source it takes it from (``TaskFactory(...).load_dataset()``,
``scripts/evaluate_results.py:422``), at the real published size. No toy input: on ``convex_hull``
that is 267,021 points, and the whole point of the number is that it is the size being graded.

WHY IT PROFILES THE WHOLE SOLVER MODULE AND NOT ONLY ``Solver.solve``
---------------------------------------------------------------------
The arena wraps exactly one callable (``profiler.py:122``), so its table covers ``solve`` and
nothing it calls. Measured on the live ``convex_hull`` champion
(``fullctx-probe/runs/convex_hull/run/nodes/node_0/solver.py``), solve-only profiling is USELESS:
three rows, 99.9 % of the time on ``return self._fast(points)``, naming nothing. The arena's agent
lives with that because it can follow up -- ``profile_lines`` on chosen lines, ``profile`` on a
different input, as often as its budget allows. Ours gets ONE fixed call and no follow-up, so this
registers every function defined in the candidate's own files. That is more than the arena hands its
agent per call and less than what the arena's agent can obtain across a session; it is the closest
single-shot equivalent, and it is recorded here so the asymmetry is read from the code and not
guessed.

WHY THE OUTPUT IS A GLOBAL TOP-N AND NOT ``LineProfiler.print_stats()``
-----------------------------------------------------------------------
``looplab/core/context_budget.py:20`` caps a developer-command result at ``RESULT_CAP = 4000``
characters, and ``dev_commands.py::_project`` keeps the TAIL of stdout. Measured: the full
``print_stats()`` for this six-function solver is ~10 KB, so the agent would have received its last
third with the header, the timings and the hottest function all cut off. This prints the N hottest
lines across all profiled functions -- N=25, which is also what the arena's own ``profile`` shows
("Shows the 25 most time-consuming lines") -- grouped under their function, and keeps the whole
thing under the cap.
"""
from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import os
import signal
import sys
import time
from pathlib import Path

# BEFORE any AlgoTune import. `load_dataset` regenerates a missing dataset by RUNNING the reference
# solver over a fresh instance sweep, which is minutes of work and would silently turn a profile
# call into a dataset build; `evaluate_results.py` sets the same flag around its own load.
os.environ.setdefault("SKIP_DATASET_GEN", "1")


class _Budget(Exception):
    """The solve ran past its allowance. Raised from SIGALRM."""


@contextlib.contextmanager
def _deadline(seconds: float):
    if seconds <= 0:
        yield
        return

    def _fire(signum, frame):
        raise _Budget()

    old = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _describe(problem) -> str:
    """One line about the instance, so the agent can see it profiled the real size."""
    def one(v):
        shape = getattr(v, "shape", None)
        if shape is not None:
            return f"{getattr(v, 'dtype', '?')} array {tuple(shape)}"
        if isinstance(v, (list, tuple)):
            inner = f", first elem {type(v[0]).__name__}" if v else ""
            return f"{type(v).__name__} of {len(v)}{inner}"
        if isinstance(v, (int, float, str, bool)) or v is None:
            return repr(v)[:40]
        return type(v).__name__

    if isinstance(problem, dict):
        return "; ".join(f"{k}: {one(v)}" for k, v in list(problem.items())[:8])
    return one(problem)


def _load_instance(root: Path, task: str, subset: str, index: int):
    """The instance, from the SAME source the scorer uses.

    `looplab_eval.py` shells out to `scripts/evaluate_results.py`, which at line 422 does
    `task_instance.load_dataset()` and takes the train or the test generator according to
    `ALGOTUNE_EVAL_SUBSET` (patch_eval_subset.py). This reproduces those three lines; it does NOT
    import the evaluator, because `looplab_eval.py` records that importing AlgoTuner in a process
    that then builds the evaluator's pool crashes the evaluation -- profiling is a different
    process and does not build one, but the same import is kept as narrow as possible.
    """
    sys.path.insert(0, str(root))
    from AlgoTuner.config.loader import load_config
    from AlgoTuneTasks.factory import TaskFactory

    task_config = load_config().get("tasks", {}).get(task, {})
    instance = TaskFactory(task, oracle_time_limit=task_config.get("oracle_time_limit"),
                           data_dir=os.environ.get("DATA_DIR", ""))
    train_iter, test_iter = instance.load_dataset()
    chosen = train_iter if subset == "train" else test_iter
    record = None
    for i, record in enumerate(chosen):
        if i >= index:
            break
    if record is None:
        raise RuntimeError(f"the {subset} split of {task} is empty")
    return record


def _own_functions(code_dir: Path):
    """Every function defined in the candidate's OWN files, in definition order.

    Scans `sys.modules` rather than the solver module alone: `edit_surface` lets a node write more
    than one `.py`, and a helper module's hot loop is exactly the thing a single-shot profile must
    not miss. Compiled `.pyx` extensions have no Python line events and simply do not appear --
    line_profiler cannot see them, which is upstream's limitation too.
    """
    seen, found = set(), []
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        try:
            if code_dir not in Path(path).resolve().parents:
                continue
        except OSError:
            continue
        for _, obj in vars(module).items():
            candidates = [obj]
            if inspect.isclass(obj) and getattr(obj, "__module__", None) == module.__name__:
                candidates = [v.__func__ if isinstance(v, (staticmethod, classmethod)) else v
                              for v in vars(obj).values()]
            for fn in candidates:
                if not inspect.isfunction(fn):
                    continue
                if getattr(fn, "__module__", None) != module.__name__:
                    continue
                if id(fn.__code__) in seen:
                    continue
                seen.add(id(fn.__code__))
                found.append(fn)
    return found


def _table(stats, max_lines: int, budget: int, source_width: int = 52) -> str:
    """The N hottest lines across every profiled function, grouped under their function, SHRUNK
    until it fits `budget` characters.

    `% Time` is the share of the WHOLE profiled solve, not of the enclosing function: with several
    functions on the table a per-function percentage cannot be compared across rows, and comparing
    rows is the only thing this table is for.

    THE SHRINK LOOP IS NOT DEFENSIVE PROGRAMMING, it repairs a measured failure. Each function on
    the table costs two header lines, so the size of a 25-line table depends on how those lines are
    SPREAD. Measured 2026-08-27 on a 30-helper solver whose hot lines land one per function: 6,300
    characters, against `RESULT_CAP = 4000` (`looplab/core/context_budget.py:20`) applied by
    `dev_commands.py::_project`, which keeps the TAIL of stdout. The agent would have been handed
    the coldest helpers and lost the header, the timings and the 50 % line at the top. Fewer rows
    that arrive beat more rows that are cut from the wrong end.
    """
    unit = stats.unit                       # seconds per tick
    per_fn, total = {}, 0.0
    for (filename, start, name), entries in stats.timings.items():
        rows = [(lineno, hits, ticks * unit) for lineno, hits, ticks in entries if hits]
        if not rows:
            continue
        per_fn[(filename, start, name)] = rows
        total += sum(r[2] for r in rows)
    if not per_fn or total <= 0:
        return ("(no Python line events: nothing of your own code ran under the profiler -- an "
                "entry point that is a compiled extension, or a solve() that returned before "
                "reaching any instrumented line)")
    hottest = sorted((r[2] for rows in per_fn.values() for r in rows), reverse=True)

    sources = {}
    def source(filename, lineno):
        if filename not in sources:
            try:
                sources[filename] = Path(filename).read_text(encoding="utf-8",
                                                             errors="replace").splitlines()
            except OSError:
                sources[filename] = []
        lines = sources[filename]
        text = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else "?"
        return text[:source_width - 1] + "…" if len(text) > source_width else text

    order = sorted(per_fn.items(), key=lambda kv: -sum(r[2] for r in kv[1]))
    every = sum(len(rows) for rows in per_fn.values())

    def render(limit: int) -> str:
        cutoff = hottest[min(limit, len(hottest)) - 1]
        out, shown = [], 0
        for (filename, _start, name), rows in order:
            picked = []
            for row in sorted(rows, key=lambda r: -r[2]):
                if shown >= limit or row[2] < cutoff:
                    break
                picked.append(row)
                shown += 1
            if not picked:
                continue
            fn_total = sum(r[2] for r in rows)
            out.append(f"\n{Path(filename).name}::{name}  "
                       f"{fn_total * 1e3:.1f} ms, {100 * fn_total / total:.0f}% of profiled time")
            out.append("  Line   Hits    Time_ms   PerHit_us   % Source")
            for lineno, hits, seconds in sorted(picked, key=lambda r: r[0]):
                out.append(f"{lineno:6d} {hits:6d} {seconds * 1e3:10.3f} "
                           f"{seconds / hits * 1e6:11.2f} {100 * seconds / total:3.0f} "
                           f"{source(filename, lineno)}")
        if every > shown:
            out.append(f"({every - shown} colder lines not shown)")
        return "\n".join(out)

    limit = min(max_lines, len(hottest))
    rendered = render(limit)
    while len(rendered) > budget and limit > 1:
        limit -= 1
        rendered = render(limit)
    return rendered


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--task", required=True)
    ap.add_argument("--solver", default="solver.py",
                    help="Candidate entry file, resolved against the current directory.")
    ap.add_argument("--subset", choices=("train", "test"), default="train",
                    help="Which half to draw the instance from. TRAIN, for the same reason "
                         "looplab_eval.py scores on train: the graded split is not for iterating on.")
    ap.add_argument("--instance", type=int, default=0,
                    help="Index into that split. Pinned to 0 by the task card, because the model "
                         "cannot pass arguments to a developer command.")
    ap.add_argument("--solve-timeout", type=float, default=45.0,
                    help="Wall seconds allowed to EACH of the three solve() calls (cold, warm, "
                         "profiled). On expiry the partial profile is still printed, which names "
                         "the line the solver was stuck inside.")
    ap.add_argument("--max-lines", type=int, default=25,
                    help="Hottest lines to print. 25 is what the arena's own `profile` shows; the "
                         "table is shrunk below this if it would not fit --max-chars.")
    ap.add_argument("--max-chars", type=int, default=3200,
                    help="Ceiling on the whole printed report. Default 3200 against the 4000-char "
                         "`RESULT_CAP` a developer-command result is clipped to, leaving room for "
                         "the ~450 characters of argv/exit-code header the tool prepends.")
    args = ap.parse_args()

    root = args.algotune_root.resolve()
    code_dir = Path.cwd().resolve()
    solver_path = (code_dir / args.solver).resolve()
    if not solver_path.is_file():
        print(f"profile: no {args.solver} in {code_dir}")
        return 1

    # THE SETUP PHASE IS MUTED, and that is a budget decision rather than tidiness. AlgoTune's
    # loader prints to STDOUT -- "Successfully loaded config", a HuggingFace snapshot banner,
    # progress bars -- ~1.5 KB of it, against a 4000-character `RESULT_CAP` whose stdout share
    # `dev_commands.py::_project` keeps by TAIL. Left alone that banner does not merely waste the
    # budget, it pushes the profile's own header out of the window.
    noise = io.StringIO()
    load_seconds = 0.0
    try:
        with contextlib.redirect_stdout(noise), contextlib.redirect_stderr(noise):
            import logging
            logging.disable(logging.CRITICAL)
            # OFFLINE FIRST. `AlgoTuneTasks/hf_dataset.py::ensure_hf_dataset` calls
            # `snapshot_download` on every load; with the repo already in `.hf_datasets` that is a
            # network round-trip for a file we have. Measured 2026-08-27 on convex_hull: 1.08 s
            # online against 0.43 s with `HF_HUB_OFFLINE=1`, which returns the existing local dir.
            # Falls back to an online attempt so a checkout that has NOT downloaded the task still
            # works -- the flag is a saving, not a requirement.
            started = time.perf_counter()
            os.environ["HF_HUB_OFFLINE"] = "1"
            try:
                record = _load_instance(root, args.task, args.subset, args.instance)
            except Exception:
                os.environ.pop("HF_HUB_OFFLINE", None)
                record = _load_instance(root, args.task, args.subset, args.instance)
            load_seconds = time.perf_counter() - started

            from AlgoTuner.utils.solver_loader import load_solver_module, with_working_dir
            # THE CANDIDATE'S OWN DIRECTORY ON `sys.path`, because the SCORER puts it there.
            # `scripts/evaluate_results.py:396` does `sys.path.insert(0, str(code_dir))` before it
            # imports the solver, so a node may split its work across `solver.py` and a sibling
            # module and still be scored. `load_solver_module` alone does NOT do this -- it loads
            # one file by path -- and without the line below this command failed with
            # `ImportError: No module named 'helper'` on a two-file solver the evaluator accepts
            # (measured 2026-08-27). A profiler that refuses inputs the grader accepts is worse
            # than no profiler: it reads as "your solver is broken".
            sys.path.insert(0, str(code_dir))
            with with_working_dir(code_dir):
                module = load_solver_module(code_dir, solver_filename=solver_path.name)
    except Exception as exc:                                  # noqa: BLE001
        print(f"profile: could not set up: {type(exc).__name__}: {exc}")
        print(noise.getvalue()[-800:])
        return 1

    solver_class = getattr(module, "Solver", None)
    if solver_class is None:
        print(f"profile: no class `Solver` in {args.solver}")
        return 1
    solve = getattr(solver_class(), "solve", None)
    if not callable(solve):
        print(f"profile: `Solver` in {args.solver} has no callable solve()")
        return 1

    problem = record["problem"] if isinstance(record, dict) and "problem" in record else record

    # THREE CALLS ON COPIES, THE LAST ONE PROFILED, AND THE FIRST TWO ARE NOT PADDING.
    #
    # (a) A COLD CALL IS NOT THE STEADY STATE. These instances arrive as `np.memmap` -- convex_hull
    # is 267,021x2 float64 -- and the first solve() faults them in. Measured 2026-08-27 on the live
    # champion: 7.0 ms, then 2.9 and 2.9 ms. A numba or DaCe solver pays far more than that on call
    # one, and it pays it inside whichever line happened to trigger the compile. Profiling call one
    # would put that cost on the table as if it were the algorithm.
    # (b) A WARM UN-PROFILED NUMBER MAKES THE TABLE READABLE. line_profiler charges per Python line
    # event and nothing for time inside C: measured on a 3e6-iteration pure-Python loop it is 10.5x,
    # and on this numpy solver 1.2x. Printing both lets the agent see which regime its solver is in
    # instead of being told a range.
    # (c) THE PROFILED CALL GETS THE PRISTINE INSTANCE. A solver may mutate `problem` in place; the
    # arena's timing harness hands each run its own, so the measured run must not read leftovers.
    import copy

    def _timed(payload):
        """(seconds, note). Never raises: a solver that dies is a thing to report, not to crash on."""
        started_at = time.perf_counter()
        try:
            with _deadline(args.solve_timeout):
                solve(payload)
            return time.perf_counter() - started_at, None
        except _Budget:
            return None, f"hit its {args.solve_timeout:g}s allowance"
        except Exception as exc:                              # noqa: BLE001
            return None, f"raised {type(exc).__name__}: {exc}"

    _cold_seconds, cold_note = _timed(copy.deepcopy(problem))
    warm_seconds, warm_note = _timed(copy.deepcopy(problem))

    from line_profiler import LineProfiler
    profiler = LineProfiler()
    registered = _own_functions(code_dir)
    for fn in registered:
        profiler.add_function(fn)
    profiler.add_function(solve.__func__ if inspect.ismethod(solve) else solve)

    profiler.enable_by_count()
    profiled_seconds, profiled_note = _timed(problem)
    profiler.disable_by_count()

    head = [f"profile: {args.task} / {args.subset} split / instance {args.instance}, "
            f"the real graded size (loaded in {load_seconds:.2f}s)",
            f"input: {_describe(problem)}"]
    warm = f"{warm_seconds * 1e3:.1f} ms" if warm_seconds is not None else f"n/a ({warm_note})"
    if profiled_seconds is None:
        head.append(f"solve(): {warm} warm and un-profiled; the PROFILED run {profiled_note}, so "
                    f"the table below is partial -- it still names the line it was inside.")
    else:
        ratio = (f", {profiled_seconds / warm_seconds:.1f}x" if warm_seconds else "")
        head.append(f"solve(): {warm} warm and un-profiled, {profiled_seconds * 1e3:.1f} ms under "
                    f"LineProfiler{ratio}. The profiler charges per PYTHON LINE and nothing for "
                    f"time spent inside C, so read the shares, not this total.")
    if cold_note:
        head.append(f"NOTE: the first (cold) call {cold_note}.")
    head.append(f"{len(registered)} function(s) of your own code instrumented; a line that calls "
                f"another instrumented function INCLUDES that function's time, so shares can sum "
                f"past 100%.")
    text = "\n".join(head)
    print(text)
    print(_table(profiler.get_stats(), args.max_lines, args.max_chars - len(text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
