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
import hashlib
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


def dataset_n(task: str, root: str = f"{BENCH}/AlgoTune"):
    """The instance size the dataset was built at, off the same file name (`..._n8_...`).

    Needed to time the reference OUTSIDE the harness at the size the harness uses. Measured
    2026-09-07 at these very sizes, three generated instances each, five repeats, one lane, warm:

        task             cached per-instance   in-process   cached / in-process
        pde_heat1d            146.4 ms           72.4 ms          2.02
        edge_expansion         45.4 ms           30.4 ms          1.49

    So between a third and a half of what the ruler calls "the reference's time" is not the
    reference solving anything. That is not a defect -- isolation, warmups and validation are what
    make the number reproducible -- but it is the denominator every speedup on this box is divided
    by, and it had never been separated into its parts.
    """
    for path in glob.glob(f"{root}/.hf_datasets/*/data/{task}/{task}*_T*ms_*"):
        got = re.search(r"_n(\d+)_", os.path.basename(path))
        if got:
            return int(got.group(1))
    return None


def in_process_ms(task: str, n: int, root: str = f"{BENCH}/AlgoTune", repeats: int = 3,
                  instances: int = 3, timeout: float = 240.0):
    """Median ms to solve one generated instance of size `n`, in a plain process, warm.

    Run in a SUBPROCESS under the bench interpreter, for §299's reason: this box has two Pythons and
    the one that scores is `AlgoTune/.venv/bin/python`. A number timed under the other interpreter
    is the mistake that cost a week.
    """
    code = (
        "import importlib.util,json,statistics,sys,time\n"
        f"spec=importlib.util.spec_from_file_location('t','{root}/AlgoTuneTasks/{task}/{task}.py')\n"
        "mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "cls=[o for o in vars(mod).values() if isinstance(o,type)"
        " and getattr(o,'__module__','')==mod.__name__"
        " and hasattr(o,'solve') and hasattr(o,'generate_problem')]\n"
        "if not cls: print('{}'); raise SystemExit\n"
        "inst=cls[-1]()\n"
        f"ps=[inst.generate_problem(n={n}, random_seed=s) for s in range({instances})]\n"
        "for p in ps: inst.solve(p)\n"
        "ts=[]\n"
        f"for _ in range({repeats}):\n"
        "    for p in ps:\n"
        "        t=time.perf_counter(); inst.solve(p); ts.append((time.perf_counter()-t)*1000)\n"
        "print(json.dumps({'ms': statistics.median(ts)}))\n")
    try:
        got = subprocess.run([bench_python(), "-c", code], capture_output=True, text=True,
                             timeout=timeout, cwd=root)
        return json.loads(got.stdout.strip().splitlines()[-1]).get("ms")
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _cached_median_ms(task: str, subset: str, key: str = "w22x1r3"):
    """The median of the cached per-instance timings this reading is divided by.

    §353. `key` defaulted to `w22x1r3` and every caller took the default, so a SERIAL reading
    recorded the WIDE median as its denominator -- 45.48 ms on `edge_expansion` where the serial
    cache says 28.21. The field is called `cached_ms` and named itself "what this reading divided
    by", which it was not. Caught by §352's cross-check firing on a reading taken forty minutes
    after that check was written, with the regime label correct: the label was right and the
    DENOMINATOR was the one lying. See the correction to §352 in docs/56.
    """
    path = f"{baseline_dir()}/{task}__{subset}__{key}.json"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    times = sorted(float(v) for v in data.values() if isinstance(v, (int, float)))
    return times[len(times) // 2] if times else None


def reference_module(task: str, probe_root: str = f"{BENCH}/model-probes") -> tuple[str, str]:
    """`(path, sha256[:12])` of the delivered reference this self-check will inline.

    §346. Every OTHER input to a reading is now on the row -- the lane (§266), the cpus busy outside
    it (§295), the regime (§329), both halves of the denominator (§319) -- and the one input that is
    the thing being measured was not. The self-check calls itself "the reference against itself",
    and the reference it uses is whatever `sorted(glob)[0]` returns out of a probe's workspace: a
    candidate that rewrites its staged copy would move the constant with nothing on the row to say
    so. Measured 2026-09-08, so the record starts from a known state: 11 staged copies for
    pde_heat1d, 13 for discrete_log, 119 for edge_expansion, 1 for pagerank -- and ONE distinct
    version of each. Today it changes nothing; that is what makes it worth recording now.
    """
    found = sorted(glob.glob(f"{probe_root}/*/ws/{task}/reference_{task}.py"))
    if not found:
        raise FileNotFoundError(f"no delivered reference module for {task} under {probe_root}")
    sha = hashlib.sha256(Path(found[0]).read_bytes()).hexdigest()[:12]
    return found[0], sha


def probes_alive_outside_lane(proc: str = "/proc", affinity=None, mine=None) -> int | None:
    """Bench probes ALIVE on other lanes -- running or merely waiting on the gateway.

    §365. `busy_cpus_outside_lane` counts cpus in state R, and it is right to: a probe waiting on
    the model burns no core, and §295's older version that counted every pinned process read 22 on
    an idle box. Measured 2026-09-09 with FOUR probes live on the four bench lanes: state R on the
    bench lanes was **zero** and the field read **0** -- an honest answer to the question it asks.

    It is not the whole condition for a ruler reading, though. Those four probes will each start a
    twenty-two-worker evaluation at a moment nobody controls, and a self-check runs for minutes; a
    reading recorded as "quiet" can be wrecked between two of its own reps. The field's own
    docstring already concedes the near case -- "a neighbour that both starts and ends inside a
    single rep is missed" -- and this is the far one: a neighbour that is not running YET.

    Recorded beside the cpu count, not folded into it. Changing what "quiet" means would re-open
    every pooled number in the drift log; saying how many neighbours were alive lets a later reader
    separate the two kinds of quiet without touching what was already measured.
    """
    # INJECTABLE, for `lanes.probes`' reason: the scan is only testable against a fake `/proc`, and
    # the two mutations that matter -- counting our own probe as a neighbour, and counting a shell
    # or a tool as the engine -- cannot go red without one.
    affinity = affinity or os.sched_getaffinity
    try:
        mine = affinity(0) if mine is None else mine
    except OSError:
        return None
    if not mine:
        return None
    root = f"{BENCH}/model-probes"
    alive = 0
    for pid in sorted(os.listdir(proc)):
        if not pid.isdigit():
            continue
        try:
            with open(f"{proc}/{pid}/cmdline", "rb") as fh:
                argv = fh.read().decode("utf-8", "replace").split("\0")
            aff = affinity(int(pid))
        except (OSError, ValueError):
            continue
        argv = " ".join(a for a in argv if a)
        if not argv or root not in argv:
            continue
        if "looplab.cli" not in argv:
            continue                       # the engine itself, not its shell or a tool
        if aff and not (aff & mine):
            alive += 1
    return alive


def build_solver(task: str, out_dir: str, probe_root: str = f"{BENCH}/model-probes") -> str:
    """Write a SELF-CONTAINED `solver.py` whose `solve()` is the reference's own.

    Inlined rather than imported: `--solver-file-only` copies one file, and an import of the
    reference module comes back `solver_unloadable` with `eval_seconds` 1.7.
    """
    found, _sha = reference_module(task, probe_root)
    body = Path(found).read_text(encoding="utf-8")
    got = re.search(r"^class (\w+)\(Task\)", body, re.M)
    if not got:
        raise ValueError(f"{found} has no `class X(Task)` to delegate to")
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


def observed_regime(task: str, subset: str, evals=None) -> str | None:
    """The regime key the run ACTUALLY divided by, taken from the evaluation's own report.

    Not the requested one. §305 asked for `ALGOTUNE_EVAL_WORKERS=1`, got twenty-two, wrote
    `__w22x1r3` and reported a number that read like a one-worker measurement; the override that
    caused it is gone (§306), but recording the intent would reintroduce the same class of lie for
    free. What the arena resolved is what the reading was divided by.

    §314 is why this belongs in the row at all: max_clique_cpsat reads 1.5291 at twenty-two workers
    and 0.9922 at one, on the same quiet box against baselines built in each regime. Two rows
    carrying the same task name and no regime are not a series -- they are two different questions.
    """
    # THE RUN'S OWN ANSWER FIRST (§350). Globbing the cache by mtime does NOT say what this run
    # divided by: an evaluation that READS a cached entry leaves its mtime untouched, so the newest
    # file is simply whichever regime was minted last. Both regimes exist for all four sweep tasks,
    # and the serial entries were written on 09-06 while the wide ones date from 08-31 -- so every
    # WIDE reading taken since has been stamped `lane22r3`. Four of them, taken on a quiet box on
    # 2026-09-08, went into the SERIAL pool: the exact mixing §314 forbids, produced by the field
    # that exists to prevent it. `looplab_eval.py` stamps `eval_regime()` on its own output
    # (`out.setdefault("eval_regime", ...)`), which is what the arena actually resolved.
    for row in evals or []:
        key = ((row or {}).get("eval_regime") or {}).get("key")
        if key:
            return str(key).lstrip("_") or None
    # NO FALLBACK TO THE CACHE LISTING. Returning a guess here is what put four wide readings in the
    # serial pool; a row that cannot name its regime is handled by `sweep_claims` (§340), and that
    # path is honest. None means "this run did not say".
    return None


def one_eval(task: str, solver: str, lane: str, subset: str, timeout: float = 900.0) -> dict:
    cache = baseline_dir()
    env = dict(os.environ,
               DATA_DIR=f"{BENCH}/AlgoTune/.hf_datasets/oripress__AlgoTune/data",
               ALGOTUNE_BASELINE_CACHE_DIR=cache,
               # THE THIRD SILENT OVERRIDE IN THIS ONE FUNCTION. `sys.executable` was §299 and the
               # baseline cache was §295; this one forced `ALGOTUNE_EVAL_WORKERS=auto` over
               # whatever the caller set. It blocked §305's own registered experiment: a run with
               # `ALGOTUNE_EVAL_WORKERS=1` wrote `__w22x1r3` and read 1.28, so the "one worker
               # instead of twenty" test measured twenty workers and said nothing. `eval_regime`
               # honours the variable correctly -- 1 keys `__lane22r3`, 4 keys `__w4x1r3` -- the
               # override was here.
               ALGOTUNE_MIN_TIMEOUT_S=os.environ.get("ALGOTUNE_MIN_TIMEOUT_S", "120"),
               ALGOTUNE_EVAL_WORKERS=os.environ.get("ALGOTUNE_EVAL_WORKERS", "auto"))
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

def cpus_counted_for(pid: int, mine: set, total: int) -> set:
    """The CPUs ONE process contributes to the load figure -- empty when it contributes none.

    §390. The per-process decision, exposed because it is the only thing about this rule that can be
    checked deterministically. Three fixtures for the same claim have now failed for three different
    reasons -- an absolute count red under a second suite, a delta red under a live probe, and a
    "pick free CPUs and watch them" red when the OTHER pytest suite, pinned to the service lane,
    took the very CPUs the fixture had just measured as free. Each version asked the box a question
    about a moment; the rule is about a PROCESS, and this is that question.

    The two halves, both load-bearing: a process sharing our own lane is not "outside" it, and a
    process that is not RUNNING is not load (§295 counted 23 dead forkservers and read 22 on an idle
    box).
    """
    try:
        other = os.sched_getaffinity(pid)
        if not (other and len(other) < total and not (other & mine)):
            return set()
        stat = open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace").read()
        # After the ") " so a process whose NAME contains a bracket cannot shift the field.
        if stat.split(") ")[-1].split()[0] != "R":
            return set()
        return set(other)
    except (OSError, ValueError, IndexError, ProcessLookupError):
        return set()


def busy_cpus_outside_lane_set() -> set | None:
    """The CPUs themselves, not their count -- see `busy_cpus_outside_lane` for the rule.

    §388. Split out because a COUNT cannot be checked against a live box. The test for "an idle
    pinned neighbour is not counted" compared the count before and after spawning one, and a probe
    whose workers arrived between the two readings made it fail (0 -> 8) while the rule it is about
    held perfectly. With the set, the claim is testable directly: the idle child's own CPUs must not
    appear, whatever else the box is doing.
    """
    try:
        mine = os.sched_getaffinity(0)
        total = os.cpu_count() or 0
        if not mine or not total or len(mine) >= total:
            # §388. `None` FOR AN UNPINNED PROCESS, which is what this function's own docstring has
            # always said ("None means the question is not answerable here") and what the code did
            # NOT do: it answered `set()`, i.e. zero, and only an EMPTY affinity -- a state Linux
            # does not produce -- got None. A row recording "0 cpus busy outside the lane" from a
            # box with no lane makes a claim about the box that was never measured, which is §313's
            # defect wearing the field that was added to fix it. The recorder already drops a None
            # (`max(seen) if seen else None`), so declining costs nothing.
            return None
        seen: set[int] = set()
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                seen |= cpus_counted_for(int(pid), mine, total)
        return seen
    except OSError:
        return None


def busy_cpus_outside_lane() -> int | None:
    """CPUs occupied by PINNED work outside this process's own affinity set.

    The same count `baseline_manager` writes into a baseline's `.provenance.json` (§295), computed
    the same way on purpose: a reading and the ruler it is compared against have to describe the
    box in the same units, or the comparison is between two different sentences.

    CPUs, not processes. Counting distinct affinity SETS counts per-core workers and reads 46 on a
    box running two lanes; the union of their CPUs reads 22, which is the number that predicts the
    slowdown. None means the question is not answerable here (we are not pinned at all).

    AND ONLY WHAT IS ACTUALLY RUNNING. §295's version -- still deployed in `baseline_manager` when
    this was written -- counted every process whose affinity was a disjoint subset, running or not.
    Measured 2026-09-06 on an idle box: it read 22, the same number it read while a pinned neighbour
    was burning eleven cores, because 23 orphaned `multiprocessing` forkservers from a run six hours
    dead were still sitting pinned to that lane. A field that reads 22 for both answers is not a
    measurement of load; it is a measurement of how many workers once existed. State R is the
    difference between the two readings, and it is the whole content of the field.
    """
    got = busy_cpus_outside_lane_set()
    return None if got is None else len(got)


def append_reading(path, task: str, subset: str, values, median: float, stamp=None,
                   lane: str | None = None, busy: int | None = None,
                   regime: str | None = None, solver_ms: float | None = None,
                   cached_ms: float | None = None, reference_sha: str | None = None,
                   reference_from: str | None = None, interpreter: str | None = None,
                   neighbours_alive: int | None = None) -> dict:
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

    AND THE CPUs BUSY OUTSIDE THAT LANE, because the lane alone is not the condition. Measured
    2026-09-06: `discrete_log` read 0.9380 at 06:38 while three sibling lanes were self-checking,
    and 1.0274 on a quiet box two hours later -- a 9.5 % swing with the lane, the interpreter, the
    baseline and the task all held fixed. Without this field that pair is a series showing drift;
    with it, it is one reading taken under load and one taken without. §295 put the same count on
    the BASELINE's sidecar and stopped at the write side; the reading is the other half.

    `busy` is passed IN, sampled while the evaluations were running. Sampling it here would read
    the box AFTER the work finished, which is exactly when the neighbours have gone quiet.
    """
    row = {"stamp": stamp or datetime.datetime.now().isoformat(timespec="seconds"),
           "task": task, "subset": subset, "lane": lane,
           "busy_cpus_outside_lane": busy, "regime": regime,
           # THE TWO HALVES OF THE DENOMINATOR, recorded rather than recomputed: the cached
           # per-instance median this reading divided by, and the same reference timed in a plain
           # process at the dataset's own size. §319 measured the split at 33-49 % across four
           # tasks; a sweep that had to re-time it would either cost a minute per task or go back
           # to quoting the number from a comment.
           "cached_ms": (round(float(cached_ms), 4) if isinstance(cached_ms, (int, float)) else None),
           "solver_ms": (round(float(solver_ms), 4) if isinstance(solver_ms, (int, float)) else None),
           # AND WHICH REFERENCE IT WAS MEASURED AGAINST (§346) -- the last unrecorded input, and
           # the one the reading is named after. `reference_from` is the probe whose workspace the
           # module came out of, so a reading can be traced without re-globbing a tree that may be
           # gone by then.
           "reference_sha": reference_sha, "reference_from": reference_from,
           # AND THE INTERPRETER (§349). §299 is the section about a wrong one surviving three
           # sweeps, four reported findings and six refuted hypotheses -- "a wrong instrument
           # reproduces its own error perfectly" -- and its fix stamped the interpreter on the
           # BASELINE's sidecar. The reading is the other half and never got it: the nine conda-era
           # rows carry `interpreter: conda (WRONG -- see §299)` only because they were marked BY
           # HAND afterwards. They are excluded from today's verdict solely because they predate
           # `busy_cpus_outside_lane`, which is an accident, not a rule.
           "interpreter": interpreter,
           # AND HOW MANY NEIGHBOURS WERE ALIVE (§365). `busy_cpus_outside_lane` answers "is another
           # lane burning cpu NOW"; this answers "is another probe about to". Both, or a reading
           # taken beside four sleeping probes is filed as quiet.
           "neighbours_alive": neighbours_alive,
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
        # SAMPLED BETWEEN THE REPS, not after the loop. By the time a reading is written the
        # neighbours have usually stopped, which is how the 06:38 rows came to say nothing about
        # the three lanes that were running beside them. Taking the MAX over the samples answers
        # "was this reading taken on a busy box", which is the question; a neighbour that both
        # starts and ends inside a single rep is missed, and that is not the kind that moves a
        # median by 9 %.
        busy_seen = []
        seen_evals = []
        for _ in range(max(1, args.reps)):
            busy_seen.append(busy_cpus_outside_lane())
            row = one_eval(args.task, solver, args.lane, args.subset)
            busy_seen.append(busy_cpus_outside_lane())
            why = refused(row)
            if why:
                bad.append(why)
                continue
            seen_evals.append(row)
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
    # THE REGIME THE RUN ACTUALLY RESOLVED, same source as §350's fix. Passing nothing here is
    # what made every serial reading claim a wide denominator.
    ran_regime = observed_regime(args.task, args.subset, seen_evals)
    cached = (_cached_median_ms(args.task, args.subset, ran_regime) if ran_regime
              else _cached_median_ms(args.task, args.subset))
    if target and cached:
        print(f"  (the dataset name says the reference took {target:.0f} ms per instance on the "
              f"machine that BUILT it; our cached baseline says {cached:.1f} ms, "
              f"{target / cached:.1f}x that machine's speed)")
    # WHAT THE DENOMINATOR IS MADE OF. Every speedup on this box divides by the cached per-instance
    # time, and that number is not the reference solving anything: measured 2026-09-07 at the
    # dataset's own instance size, pde_heat1d reads 146.4 ms cached against 77.0 ms in a plain
    # process, edge_expansion 45.4 against 30.4. Isolation, warmups and validation are what make the
    # cached number reproducible, so this is not a defect -- but it had never been separated into
    # its parts, and a reader comparing a candidate's speedup against "the reference's time" was
    # comparing it against something roughly twice that.
    n_size = dataset_n(args.task)
    direct = in_process_ms(args.task, n_size) if (n_size and cached) else None
    if direct and cached and direct < cached:
        print(f"  (the cached per-instance median is {cached:.1f} ms; the same reference timed "
              f"in-process at the dataset's own n={n_size} reads {direct:.1f} ms, so "
              f"{100 * (cached - direct) / cached:.0f} % of the denominator is harness overhead)")

    if share:
        # RESTORED. Moving the cache line in dropped this guard for one edit, and without it a run
        # whose `instance_share` came back empty prints "per-instance work is 0 %" -- a measurement
        # where there was none.
        print(f"  (per-instance work is {100 * share:.0f} % of an evaluation's wall clock, so "
              f"`eval_seconds` cannot see this drift at all)")
    if args.record:
        seen = [b for b in busy_seen if b is not None]
        # THE REFERENCE IS LOOKED UP AGAIN, not carried down from `build_solver` -- the run may
        # have taken minutes and a probe can restage its workspace in that time. Re-reading here
        # records what is on disk NOW; a mismatch with what was inlined would mean the reading is
        # already unattributable, and a stale carried value would hide that.
        try:
            ref_from, ref_sha = reference_module(args.task)
            ref_from = ref_from.split("/model-probes/", 1)[-1].split("/")[0]
        except (FileNotFoundError, OSError):
            ref_from = ref_sha = None
        append_reading(args.record, args.task, args.subset, vals, median, args.stamp,
                       args.lane, max(seen) if seen else None,
                       ran_regime, direct, cached,
                       reference_sha=ref_sha, reference_from=ref_from,
                       interpreter=bench_python(),
                       neighbours_alive=probes_alive_outside_lane())
        print(f"  recorded to {args.record}")
    if said is not None and abs(median - said) > 0.02:
        print("  DRIFT: the cached baseline and today's box no longer agree. Within one task this "
              "cancels (every probe is divided by the same cached baseline); across time on one "
              "task it does not, which is what arm A's re-timed constants are compared over.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
