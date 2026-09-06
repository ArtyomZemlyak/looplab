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
import datetime
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


def dataset_target_ms(task: str, root: str = f"{BENCH}/AlgoTune"):
    """The reference time the machine that BUILT the dataset hit, off the file name (`..._T100ms_...`).

    NOT a measurement of this box -- `make_task.py` says so in as many words -- but the only
    cross-task yardstick there is, and it is what made §292's anomaly legible. Every task's dataset
    here says `T100ms`, so the ratio of our own cached baseline to 100 ms says how much faster this
    box is on that task than the dataset's was:

        edge_expansion   45.4 ms  ->  2.2x faster
        pde_heat1d      146.4 ms  ->  0.7x
        discrete_log      2.2 ms  ->  46x
        pagerank        109.1 ms  ->  0.9x   <- the only one that barely beats the target

    Printed beside the reading so the next person does not have to go and find it.
    """
    # THE GLOB WAS DOING THE REGEX'S JOB, AND NEITHER KNEW IT. `{task}_T*ms_*` already forbids
    # anything between the task name and the `_T` field, so a mutation loosening the regex to a bare
    # `(\d+)` could not be caught -- and the fixture I wrote to catch it used `pagerank_v2_T100ms_`,
    # a name this glob will never return. One of the two has to decide the shape; it is the regex,
    # which can say which number it means.
    for path in glob.glob(f"{root}/.hf_datasets/*/data/{task}/{task}*_T*ms_*"):
        got = re.search(r"_T(\d+)ms_", os.path.basename(path))
        if got:
            return float(got.group(1))
    return None


def _cached_median_ms(task: str, subset: str, key: str = "w22x1r3"):
    """The median of the cached per-instance timings this reading is divided by."""
    path = f"{baseline_dir()}/{task}__{subset}__{key}.json"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    times = sorted(float(v) for v in data.values() if isinstance(v, (int, float)))
    return times[len(times) // 2] if times else None


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


BENCH_PYTHON = f"{BENCH}/AlgoTune/.venv/bin/python"


def bench_python() -> str:
    """The interpreter the BENCH evaluates under -- not whichever one launched this script.

    THE WORST ERROR IN THIS SERIES CAME FROM `sys.executable`. `run_probe.sh` scores every probe with
    `$ROOT/AlgoTune/.venv/bin/python`; this file used `sys.executable`, so a self-check launched with
    `/opt/conda/bin/python` timed the reference under a DIFFERENT numpy/scipy stack. Measured
    2026-09-06 on `pagerank`: 109.999 ms under the venv against 74.6-75.3 ms under conda, a 1.46x
    gap. That gap was reported for three sweeps as a drifting ruler, chased through six refuted
    hypotheses, and finally acted on -- a correct baseline was overwritten and a probe re-scored
    under the wrong one. Both have been restored.

    Under the venv all four constants hold: pagerank 0.9727, edge_expansion 1.0027, pde_heat1d
    1.0136, discrete_log 1.0189 -- every one within 3 % of what the standing sweep says.

    So the interpreter is named here rather than inherited, and `main` refuses outright if it is
    missing: a reading taken under the wrong stack is not a worse reading, it is a different
    experiment.
    """
    return BENCH_PYTHON


def baseline_dir() -> str:
    """The cache this reading divides by: the operator's if they named one, ours otherwise.

    IT USED TO BE HARD-CODED, AND THE OVERRIDE WAS SILENT. `one_eval` passed
    `ALGOTUNE_BASELINE_CACHE_DIR=<the bench cache>` into the child's environment on top of whatever
    the caller had set, and also passed `--baseline-times-dir` pointing at the same place. So
    running this with `ALGOTUNE_BASELINE_CACHE_DIR` set to a scratch directory -- which is exactly
    how §293's registered re-timing was supposed to be taken -- produced an ordinary-looking reading
    against the REAL cache, and an empty scratch directory, with nothing said. The whole number
    depends on which cache it divided by, so the reading now names it.
    """
    return os.environ.get("ALGOTUNE_BASELINE_CACHE_DIR") or str(HERE / "algotune" / ".baseline_times")


def one_eval(task: str, solver: str, lane: str, subset: str, timeout: float = 900.0) -> dict:
    cache = baseline_dir()
    env = dict(os.environ,
               DATA_DIR=f"{BENCH}/AlgoTune/.hf_datasets/oripress__AlgoTune/data",
               ALGOTUNE_BASELINE_CACHE_DIR=cache,
               ALGOTUNE_MIN_TIMEOUT_S="120", ALGOTUNE_EVAL_WORKERS="auto")
    argv = ["taskset", "-c", lane, bench_python(),
            str(HERE / "algotune" / "looplab_eval.py"),
            "--algotune-root", f"{BENCH}/AlgoTune", "--task", task,
            "--solver", solver, "--solver-file-only",
            "--baseline-times-dir", cache,
            "--subset", subset]
    got = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    try:
        return json.loads(got.stdout)
    except ValueError:
        return {"speedup": None, "eval_seconds": None,
                "no_speedup": {"reason": "unparseable", "stdout": got.stdout[-400:]}}


DEFAULT_LOG = HERE / "algotune" / "ruler_selfcheck_log.jsonl"


def append_reading(path, task: str, subset: str, values, median: float, stamp=None,
                   lane: str | None = None) -> dict:
    """Append one dated reading, so the drift becomes a SERIES rather than a single number.

    §214 measured `edge_expansion` at 0.8861 against the sweep's 0.9847 and could say the cached
    baseline and today's box disagree -- but not WHEN they parted, because there is exactly one
    reading. §215 then showed the obvious proxy cannot help: `eval_seconds` times a different solver
    every node. A fixed-work reading taken every sweep is the only thing that can answer it, and the
    series has to start somewhere.

    The stamp is passed IN rather than read here: a caller that wants a reproducible row (a test, a
    replay) owns its own clock.

    AND THE LANE, BECAUSE A READING WITHOUT ONE IS NOT COMPARABLE TO THE NEXT. §266 measured all
    four bench lanes over six sittings: they differ by about 3 % between the extremes (lane
    22-32,70-80 reads 0.9127 against 0.9448 on 11-21,59-69), and inside a single bad sitting one
    lane can drop to 0.87 while its neighbours sit near 0.97. A drift series that does not say which
    lane it was taken on cannot tell a change in the box from a change of lane -- which is exactly
    what §265 walked into, comparing two readings whose lanes the file had never recorded.
    """
    row = {"stamp": stamp or datetime.datetime.now().isoformat(timespec="seconds"),
           "task": task, "subset": subset, "lane": lane,
           "values": [round(float(v), 6) for v in values],
           "median": round(float(median), 6)}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # HEAL A TORN TAIL BEFORE APPENDING. A row half-written by a killed process leaves the file
    # without its closing newline, and the next append lands ON THAT LINE -- destroying the new
    # reading as well as the old one. One crash would cost two readings instead of one, in a series
    # whose whole point is that readings are rare.
    try:
        if path.exists() and path.stat().st_size:
            with open(path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                torn = fh.read(1) != b"\n"
        else:
            torn = False
    except OSError:
        torn = False
    with open(path, "a", encoding="utf-8") as fh:
        if torn:
            fh.write("\n")
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def read_series(path, task: str | None = None) -> list[dict]:
    """Every recorded reading, oldest first; torn lines are skipped, not fatal."""
    out = []
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return out
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if task is None or row.get("task") == task:
                out.append(row)
    return sorted(out, key=lambda r: str(r.get("stamp") or ""))


def instance_share(task: str, eval_seconds: float, *, subset: str = "test",
                   times_dir=None) -> float:
    """What fraction of an evaluation's wall clock is the per-instance work being compared.

    WHY THIS IS HERE. `eval_seconds` from a probe's `node_evaluated` looks like the obvious way to
    catch a box that has got slower, and it is not one — for two reasons, and I went at it with the
    weaker one first.

    The weak reason is dilution, which this function computes: a hundred `edge_expansion` instances
    are **10.9 %** of an evaluation's wall clock and the rest is fixed harness overhead, so a 13 %
    move in the part being compared is 1.4 % of `eval_seconds`, inside its own p10-p90. Measured
    2026-09-04 the corpus median went 41.10 s (08-31) to 40.90 s (09-04), **−0.5 %**, and I nearly
    read that flat line as refuting the 0.8861 self-check. It does not. The share is not uniform
    either: `discrete_log` 22.5 %, `pde_heat1d` **63 %**.

    The strong reason is that `eval_seconds` **times a different solver every node**. It is the cost
    of evaluating whatever the model just wrote, not a fixed-work benchmark, so its day-to-day
    movement is the corpus's candidates changing: `discrete_log` reads 30.6 s, 57.0 s, 46.7 s on
    three consecutive days and `pde_heat1d` 54.0 -> 60.7 s, swings far larger than any drift, in a
    quantity that has no reason to be stable. §207's use of it — flat across one to four concurrent
    probes — was a statement that the harness does not collapse under load, and that much it can
    support; hardware constancy it cannot.

    Which leaves the self-check as the only instrument here comparing like with like, because both
    sides of it are the reference.
    """
    times_dir = Path(times_dir or (HERE / "algotune" / ".baseline_times"))
    path = times_dir / f"{task}__{subset}__w22x1r3.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    times = [float(v) for v in data.values() if isinstance(v, (int, float))]
    if not times or not eval_seconds:
        return 0.0
    return (sum(times) / 1000.0) / float(eval_seconds)


def refused(row: dict) -> str:
    """Why this reading is not a measurement, or "" if it is one."""
    if row.get("no_speedup"):
        # CARRY THE DETAIL, NOT JUST THE LABEL. The evaluator explains itself in full -- for a
        # regime mismatch it names both keys and what would happen -- and the first version of this
        # printed `REFUSED: baseline_regime_mismatch` four times over, discarding the sentence that
        # said WHY. That is the shape `probe_summary` was built to stop: the diagnosis exists, in a
        # field nothing read. Measured 2026-09-05, running this on the SERVICE lanes: "this
        # invocation would key its baseline '__w8x1r3', which is not on disk, while
        # edge_expansion__test__w22x1r3.json already is".
        why = row["no_speedup"] or {}
        reason = str(why.get("reason") or "refused")
        detail = str(why.get("detail") or why.get("evaluator_verdict") or "").strip()
        return f"{reason}: {detail}" if detail else reason
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
    # A BENCH LANE, NOT THE SERVICE LANES. The regime key encodes the lane WIDTH (`w22x1r3`), so an
    # 8-cpu service lane keys `__w8x1r3`, finds no cached baseline, and is refused -- correctly, by
    # §149's guard. §259's rule that every analysis runs pinned to 44-47,92-95 has exactly this
    # exception: a measurement that must happen IN the bench's regime uses a bench lane.
    ap.add_argument("--lane", default="0-10,48-58")
    ap.add_argument("--subset", default="test", choices=("train", "test"))
    ap.add_argument("--record", metavar="FILE", nargs="?", const=str(DEFAULT_LOG),
                    help="append this reading to a dated series (default: %(default)s)"
                         f" [{DEFAULT_LOG}]")
    ap.add_argument("--stamp", help="ISO timestamp for the recorded row; the caller owns the clock")
    args = ap.parse_args(argv)
    if not os.path.exists(bench_python()):
        print(f"REFUSING: the bench interpreter {bench_python()} is not on this box. A reading "
              "taken under another Python is a different experiment, not a worse one.",
              file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="ruler-selfcheck-") as tmp:
        solver = build_solver(args.task, tmp)
        vals, secs, bad = [], [], []
        for _ in range(max(1, args.reps)):
            row = one_eval(args.task, solver, args.lane, args.subset)
            why = refused(row)
            if why:
                bad.append(why)
                continue
            vals.append(float(row["speedup"]))
            if isinstance(row.get("eval_seconds"), (int, float)):
                secs.append(float(row["eval_seconds"]))

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
    share = instance_share(args.task, statistics.median(secs) if secs else 0.0,
                           subset=args.subset)
    if os.environ.get("ALGOTUNE_BASELINE_CACHE_DIR"):
        print(f"  (dividing by the cache you named: {baseline_dir()})")
    target = dataset_target_ms(args.task)
    cached = _cached_median_ms(args.task, args.subset)
    if target and cached:
        print(f"  (the dataset name says the reference took {target:.0f} ms per instance on the "
              f"machine that BUILT it; our cached baseline says {cached:.1f} ms, "
              f"{target / cached:.1f}x that machine's speed)")
    if share:
        # RESTORED. Moving the cache line in dropped this guard for one edit, and without it a run
        # whose `instance_share` came back empty prints "per-instance work is 0 %" -- a measurement
        # where there was none.
        print(f"  (per-instance work is {100 * share:.0f} % of an evaluation's wall clock, so "
              f"`eval_seconds` cannot see this drift at all)")
    if args.record:
        append_reading(args.record, args.task, args.subset, vals, median, args.stamp, args.lane)
        print(f"  recorded to {args.record}")
    if said is not None and abs(median - said) > 0.02:
        print("  DRIFT: the cached baseline and today's box no longer agree. Within one task this "
              "cancels (every probe is divided by the same cached baseline); across time on one "
              "task it does not, which is what arm A's re-timed constants are compared over.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
