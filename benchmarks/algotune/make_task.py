#!/usr/bin/env python3
"""Generate a LoopLab task spec + workspace for one AlgoTune task.

AlgoTune's agent contract is a single file — its evaluator does
``from solver import Solver; Solver()`` — so a LoopLab arm is a plain ``repo`` task
whose edit surface is exactly ``solver.py``.

What the candidate is given matches what AlgoTune gives its own agent: the problem
statement, and the reference implementation to read (its ``solve()`` defines correct
output and its ``is_solution()`` defines what will be checked). What it may write is
only the solver. The reference and the description are ``protect``ed so a candidate
cannot rewrite the thing it is being measured against.

Usage
-----
    python make_task.py --algotune-root /path/to/AlgoTune --task svm \\
                        --out-dir /path/to/workspaces

Writes ``<out-dir>/<task>/`` (the workspace) and ``<out-dir>/algotune_<task>.json``
(the LoopLab task spec), then prints the spec path.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent / "looplab_eval.py"
PROFILER = Path(__file__).resolve().parent / "looplab_profile.py"
CHECKER = Path(__file__).resolve().parent / "looplab_check.py"

SOLVER_STUB = '''from typing import Any


class Solver:
    def solve(self, problem: dict[str, Any], **kwargs) -> Any:
        """Solve the problem described in description.txt.

        Return a result that the reference implementation's is_solution() accepts,
        as fast as possible. reference_{task}.py holds the reference solve() and
        is_solution() -- read it for the contract; do not import from it.
        """
        raise NotImplementedError
'''

GOAL = (
    "Rewrite solver.py so that Solver.solve(problem) returns a result the reference "
    "is_solution() accepts on EVERY instance, as fast as possible. "
    "reference_{task}.py holds the reference solve() and is_solution() -- read it for "
    "the contract, do not import from it. The score is speedup = reference_time / "
    "your_time, measured by AlgoTune's own evaluator on this machine. A solver that is "
    "wrong on ANY instance scores 0 -- correctness first, then speed."
)


# --role-split: one experiment = one ALGORITHM, chosen by the Researcher.
#
# Measured on `discrete_log` (2026-08-20): the Developer spent 44-51 `run_probe` calls and the whole
# wall clock inside the `stages` phase without ever producing a node, benchmarking sympy against its
# own implementation at guessed problem sizes. It was not idling -- it found a real optimisation on
# the way -- but it was DECIDING WHICH ALGORITHM TO WRITE, which is the Researcher's job.
#
# The cause is upstream and visible in the hypotheses that run recorded. All five are CONDITIONAL:
# "if the modulus is small ... prefer a precomputed table", "if the modulus is large, use baby-step
# giant-step OR Pollard's rho ... CHOOSE BASED ON whether memory or time is the binding constraint
# on this machine". That is a decision tree with its branch condition left unresolved, and the only
# role holding a tool that can resolve it is the Developer. So the search moved down one layer.
#
# This clause does not tell either role the answer -- the problem size stays undisclosed, exactly as
# it is for the reference arm, which learns it from its own evaluations. It moves the CHOICE back up
# and makes an unresolved assumption a first-class experimental outcome instead of a probe loop: a
# committed guess that turns out wrong is a failed node, and a failed node is information the next
# Researcher turn reads. That is what the loop is for.
# The parenthetical is a PARAMETER because `--full-context` makes half of it false. Under that
# flag the goal states `n = 267021` as a measured fact two paragraphs earlier, and naming "the
# problem sizes" here as an example of what nobody knows contradicts it in the same prompt --
# exactly the failure `repo_developer.py` records for the probe clause: "two paragraphs
# contradicting each other is worse than either one alone", because the model is right to
# disregard a card that argues with itself. What stays unknown under --full-context is real and
# is what the sentence should point at.
_UNKNOWNS_DEFAULT = "the problem sizes, how much memory a table would take"
_UNKNOWNS_FULL_CONTEXT = ("how much memory a table would take at that n, whether this library's "
                          "implementation is the bottleneck, how the constant factors land")

ROLE_SPLIT = (
    " ONE EXPERIMENT = ONE ALGORITHM. The Researcher names exactly one concrete algorithm per "
    "experiment and commits to it -- not a decision tree, not \"A or B depending on C\". If whether "
    "that algorithm is viable depends on something nobody here knows yet ({unknowns}), the "
    "Researcher STATES THE ASSUMPTION as part of the idea and "
    "commits anyway; an assumption that turns out wrong is a FAILED EXPERIMENT, which is a result "
    "this loop is built to read and build on, not something to avoid by searching first. The "
    "Developer implements exactly the algorithm it was given. It does not pick between algorithms, "
    "does not benchmark alternatives against each other, and does not use the probe to decide WHAT "
    "to write -- the probe is for making the named algorithm actually RUN (does this import, what "
    "shape does this API return), not for choosing it. If the Developer believes the named "
    "algorithm is wrong for this task, it implements it anyway and says so in its rationale; the "
    "next experiment is where a different algorithm belongs."
)


# --deliver: the DEVELOPER half. `--role-split` moved the algorithm CHOICE back to the Researcher
# and its Researcher half worked -- the hypotheses it produced are committed experiments ("run a
# baby-step giant-step baseline as the first committed experiment") instead of the decision tree the
# control produced ("use BSGS OR Pollard's rho, choose based on ..."). Its Developer half did not:
# that run still committed a node with `files: {}`, exactly like the control.
#
# The clause was aimed wrong and this is the correction. It restricted WHY the Developer may probe
# ("the probe is for making the named algorithm actually RUN") and never told it to WRITE -- and
# "make it run" is an unlimited licence, which is what 24 probes and 216 package reads were spent
# under. Measured on the control: 19 implement generations, `write_file`/`edit_file` called ZERO
# times.
#
# What it says instead is a FACT about this task rather than a rule to obey, because a rule invites
# a workaround and a fact does not. Note the fact has to be stated PRECISELY or the model is right
# to disregard it: the Developer CAN check correctness locally (generate instances, verify
# `pow(g,x,p) == h`). What it cannot do is measure the SCORE -- the graded instances are not on the
# machine and it has no way to produce them -- and the transcripts show the search was about
# exactly that: instance sizes and timings.
# THE FALSE HALF, kept verbatim under `--deliver` so the campaign that ran on it stays
# reproducible, and REPLACED by `MEASURE` under `--full-context`.
#
# Its premise was checked against the reference arm on 2026-08-26 and does not hold. "The instances
# you are graded on are not on this machine" -- the train split IS on this machine
# (`.hf_datasets/oripress__AlgoTune/data/<task>/<task>_T100ms_n<N>_size100_train.jsonl`, 100
# instances), it is the split every node is already scored on, and AlgoTuner's own agent is handed
# the resulting `Speedup: X` with `Valid Solutions: Y%` between 17 and 61 times per task, plus
# `eval_input` 207-429 times and `profile` 58-194 times. We told our arm the metric was unknowable
# and it did the only thing left: it invented instance sizes and timed those. `convex_hull`'s real
# n is 267 021; the probes that chose its champion ran at n = 100, 1 000 and 10 000.
_DELIVER_NO_MEASURE = (
    " YOU CANNOT MEASURE YOUR OWN SCORE, AND YOU ARE NOT MEANT TO. The instances you are graded on "
    "are not on this machine and you cannot generate them; the speedup comes from the evaluator, "
    "which runs after you finish. So timing your own guesses against invented inputs measures "
    "something else -- your guess about the input -- and no amount of it brings the real number "
    "closer. (Correctness is different and you CAN check it: build a few instances yourself and "
    "verify the contract holds. Do that briefly, then stop.) "
)

_DELIVER_WRITE = (
    "YOUR OUTPUT IS THE FILE. A session that ends without writing solver.py has produced NOTHING -- "
    "not a partial result, not a finding, nothing the loop can use -- and it is thrown away. Write "
    "a correct implementation EARLY, while you still have room, and improve it after; a working "
    "solver that is only a little faster beats a perfect plan that was never written down. "
    "Probes are for questions with a yes/no answer about this environment -- does this import, what "
    "does this call return. If you have run more than a handful, you have stopped answering "
    "questions and started doing the evaluator's job for it. Where something depends on the "
    "instance sizes, PICK A REASONABLE ASSUMPTION, write it into the code as a threshold or a "
    "fallback, say so in your summary, and let the evaluation tell you whether it was right. That "
    "answer is worth more than any estimate you can make here, and it costs one experiment."
)

DELIVER = _DELIVER_NO_MEASURE + _DELIVER_WRITE


# ---------------------------------------------------------------- --full-context

# What the arena hands its OWN agent, which we had been withholding. Every number below is READ
# FROM THE DATASET, never typed in: a hand-written size goes stale in the direction that silently
# misinforms, which is the failure this clause exists to end.
_DATASET_RE = re.compile(
    r"^(?P<task>.+)_T(?P<ms>\d+)ms_n(?P<n>\d+)_size(?P<size>\d+)_(?P<subset>train|test)\.jsonl$")


def train_dataset(root: Path, task: str) -> Path | None:
    """The train split's file, or None. TEST IS NEVER RETURNED and never mounted."""
    base = root / ".hf_datasets" / "oripress__AlgoTune" / "data" / task
    if not base.is_dir():
        return None
    for path in sorted(base.glob(f"{task}_T*ms_n*_size*_train.jsonl")):
        if _DATASET_RE.match(path.name):
            return path
    return None


def _instance_shape(path: Path, limit: int = 4000) -> str:
    """The STRUCTURE of one instance -- keys, types, lengths -- not its contents.

    A prompt cannot carry 100 instances and must not try; what the agent is missing is the SHAPE,
    which is what decides the algorithm. Values are described, never quoted, so this cannot become
    a channel for memorising answers.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            record = json.loads(fh.readline() or "{}")
    except Exception:                                # noqa: BLE001 - shape is a nicety, not a gate
        return ""
    # `solve(problem)` receives the RECORD'S `problem` value, not the record. The record also
    # carries `k` (the size, already stated) and `seed`, and describing those as part of the input
    # would send the reader looking for fields the solver never sees.
    first = record.get("problem", record) if isinstance(record, dict) else record

    def describe(value, depth: int = 0) -> str:
        if depth > 3:
            return type(value).__name__
        # EXTERNAL ARRAYS ARE RESOLVED, not reported as the pointer that stands for them. The first
        # version of this printed `points: {__type__: str, npy_path: str}` for `convex_hull` -- a
        # description that tells the reader the input is a two-key dict of strings when it is a
        # (267021, 2) float64 array. Misinforming about the shape is the exact failure this clause
        # was added to end, so it had to be fixed before it shipped.
        kind = value.get("__type__") if isinstance(value, dict) else None
        if kind == "ndarray_ref":
            try:
                import numpy

                arr = numpy.load(path.parent / value["npy_path"], mmap_mode="r")
                return f"ndarray(shape={tuple(arr.shape)}, dtype={arr.dtype})"
            except Exception:                        # noqa: BLE001 - fall back to the honest name
                return "ndarray(shape unknown)"
        if kind == "ndarray_b64":
            shape = value.get("shape")
            dtype = value.get("dtype", "?")
            return f"ndarray(shape={tuple(shape) if shape else '?'}, dtype={dtype})"
        if kind == "scipy_csr_matrix_ref":
            # The shape is itself a wrapped tuple ({"__type__": "tuple", "data": [rows, cols]}) on
            # `sparse_eigenvectors_complex`, and reading it as a sequence yielded the WRAPPER'S KEYS
            # -- `csr_matrix(shape=('__type__', 'data'))`. Unwrap one level before believing it.
            shape = value.get("shape")
            if isinstance(shape, dict) and shape.get("__type__") == "tuple":
                shape = shape.get("data")
            ok = isinstance(shape, (list, tuple)) and all(isinstance(x, int) for x in shape)
            return f"scipy.sparse.csr_matrix(shape={tuple(shape) if ok else '?'})"
        if kind == "tuple":
            inner = value.get("data")
            if isinstance(inner, (list, tuple)):
                return "(" + ", ".join(describe(x, depth + 1) for x in inner[:8]) + ")"
            return "tuple"
        if isinstance(value, dict):
            items = list(value.items())
            # A DICT USED AS A MATRIX is described as one. `kcenters` hands solve() a distance map
            # whose first key alone holds 44 float entries; printing twelve of them tells the reader
            # nothing the count does not, and buries the second field.
            if len(items) > 8 and all(isinstance(v, type(items[0][1])) for _, v in items):
                return (f"{{{len(items)} keys ({describe(items[0][0], depth + 1)}) -> "
                        f"{describe(items[0][1], depth + 1)}}}")
            inner = ", ".join(f"{k}: {describe(v, depth + 1)}" for k, v in items[:8])
            return "{" + inner + ("" if len(items) <= 8 else f", +{len(items) - 8} more") + "}"
        if isinstance(value, (list, tuple)):
            if not value:
                return "[] (empty)"
            inner = describe(value[0], depth + 1)
            # RAGGED LISTS SAY SO. `edge_expansion`'s adjacency list has 4408 entries whose lengths
            # run from 5 to 32; reporting the first one's length as the shape would tell the reader
            # the graph is 11-regular, and an algorithm chosen for a regular graph is the wrong
            # algorithm. Only the length is re-described -- the element type is taken from the first.
            lengths = {len(x) for x in value if isinstance(x, (list, tuple))}
            if len(lengths) > 1:
                head = describe(value[0][0], depth + 2) if value[0] else "?"
                return (f"[{len(value)} x list[{head}], lengths vary: "
                        f"{min(lengths)}..{max(lengths)}]")
            return f"[{len(value)} x {inner}]"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return f"str(len {len(value)})"
        return type(value).__name__

    return describe(first)[:limit]


# The same cache `looplab_eval.py::DEFAULT_TIMES_DIR` resolves, by the same rule, so the goal
# card and the scorer cannot end up reading different directories.
BASELINE_TIMES_DIR = Path(
    os.environ.get("ALGOTUNE_BASELINE_CACHE_DIR")
    or (Path(__file__).resolve().parent / ".baseline_times")
)



def graded_instance_size(root: Path, task: str) -> int | None:
    """The `n` the arena actually grades this task at, or None when it cannot be read.

    The datasets are named `<task>_T<target>ms_n<N>_size<M>_<split>.jsonl` -- `N` is the instance
    parameter and `M` is how many instances. Measured 2026-08-28: kcenters is n=49 and
    edge_expansion is n=4408, against `looplab_check.py`'s default of 20. Checking a solver at 20
    when it is graded at 49 is not a check: dsKcCtl node 1 passes 1 of 3 at size 20 and 0 of 3 at
    size 49, where two of the failures are a native SIGABRT that never appears at the small size.

    Returns None rather than a guess when the layout is not what this expects, and the caller then
    omits `--size` instead of pinning a fabricated one.
    """
    import re as _re
    base = root / ".hf_datasets" / "oripress__AlgoTune" / "data" / task
    if not base.is_dir():
        return None
    sizes = set()
    for entry in base.glob(f"{task}_T*ms_n*_size*_train.jsonl"):
        m = _re.search(r"_n(\d+)_size\d+_train\.jsonl$", entry.name)
        if m:
            sizes.add(int(m.group(1)))
    # More than one is ambiguous, and an ambiguous ruler is worse than no pin.
    return sizes.pop() if len(sizes) == 1 else None

def measured_reference_ms(task: str, subset: str = "train",
                          times_dir: Path | None = None) -> tuple[float, float] | None:
    """The reference's MEASURED per-instance milliseconds on this box, as (low, high) medians.

    THE FILE NAME IS NOT A MEASUREMENT. `convex_hull_T100ms_n267021_size100_train.jsonl` says the
    dataset's builder chose n so the reference took ~100 ms *on the machine that built it*. On this
    box the same reference is 39.0 ms per instance in the campaign's own regime
    (`convex_hull__train__w22x1r3.json`, median 40.07) and 29.3 ms serially in a 22-core lane
    (`convex_hull__train__lane22r3.json`, median 29.45) -- 2.5x to 3.4x away from the file name.

    Measured harm, `fullctx-probe` 2026-08-26: the goal card quoted 100 ms as "MEASURED FROM THE
    DATASET ON THIS MACHINE -- not a guess, and not something to re-derive". The Researcher then
    sized its whole proposal against it ("filter cost is ~2 gemms = 4ms vs ~100ms reference",
    "Expected: ~8-15x"), and the solver it produced measured 29.9 ms/instance in its own probe --
    about 1.0x against the ruler its nodes are scored on. It also used 100 ms to compute that
    `eval_train` "should be ~15 s", so when that command hit its 600 s cap the run spent a further
    eight minutes probing `is_solution` to explain a timeout that had another cause entirely.

    One file per REGIME (lane width / worker pool), and this process cannot know which regime the
    scorer will run in -- it is not `taskset`-ed the way the run is. So every regime's median is
    read and the SPREAD is returned; the caller states a range when the regimes disagree rather
    than picking one and calling it the number. Returns None when nothing has been measured.
    """
    root = Path(times_dir) if times_dir is not None else BASELINE_TIMES_DIR
    medians: list[float] = []
    try:
        paths = sorted(root.glob(f"{task}__{subset}__*.json")) + \
            [root / f"{task}__{subset}.json"]
    except OSError:
        return None
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        vals = sorted(float(v) for v in raw.values()
                      if isinstance(v, (int, float)) and float(v) > 0) if isinstance(raw, dict) else []
        if vals:
            medians.append(vals[len(vals) // 2])
    if not medians:
        return None
    return (min(medians), max(medians))


def dataset_clause(root: Path, task: str) -> str:
    """The instance shape, in the goal, derived from the train split on this machine.

    Item 10 of docs/53: the loop timed probes at sizes it invented because nothing told it the real
    one. `convex_hull` is n = 267 021 and its champion was chosen from probes at n = 100, 1 000 and
    10 000 -- three orders of magnitude out, on the task whose whole score is a time.
    """
    path = train_dataset(root, task)
    if path is None:
        return ""
    m = _DATASET_RE.match(path.name)
    assert m is not None                              # train_dataset only returns matching names
    try:
        count = sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
    except OSError:
        count = 0
    shape = _instance_shape(path)
    # THE REFERENCE TIME IS MEASURED OR IT IS NOT STATED. The first version of this clause read
    # `T100ms` out of the file name and printed it under "MEASURED FROM THE DATASET ON THIS
    # MACHINE -- not a guess". It is a guess: it is the target the machine that BUILT the dataset
    # hit, and on this box the same reference is 40.07 ms (campaign regime) or 29.45 ms (serial
    # lane) -- 2.5x to 3.4x away. Measured harm on `fullctx-probe`, 2026-08-26: 360 occurrences of
    # the wrong number in one 90-minute run, a proposal sized against it ("~4 ms filter vs ~100 ms
    # reference, expected 8-15x") whose solver measured 29.9 ms/instance -- about 1.0x on the ruler
    # its nodes are scored on -- and eight further minutes spent explaining an `eval_train` timeout
    # the same 100 ms had made look inexplicable. A clause written against misinformation about the
    # task was the misinformation.
    measured = measured_reference_ms(task, "train")
    if measured is not None:
        lo, hi = measured
        cost = (f"THE REFERENCE COSTS **{lo:.0f}-{hi:.0f} ms** PER INSTANCE ON THIS BOX -- the "
                if abs(hi - lo) >= 1.0 else
                f"THE REFERENCE COSTS **{hi:.0f} ms** PER INSTANCE ON THIS BOX -- the ")
        cost += ("median of the per-instance reference timings the scorer itself divides by, not a "
                 f"number read off the dataset's file name (that name says {m.group('ms')} ms, "
                 "which is the target the machine that BUILT the dataset hit). ")
        if abs(hi - lo) >= 1.0:
            cost += ("The range is the lane regimes this box scores in; assume the slower end is "
                     "not yours. ")
        budget = f"{hi:.0f} ms"
    else:
        cost = (f"The dataset's name says the reference took about {m.group('ms')} ms per instance "
                "ON THE MACHINE THAT BUILT IT -- nothing here has measured this box, so treat that "
                "as an order of magnitude and not as your denominator. ")
        budget = f"{m.group('ms')} ms"
    clause = (
        " THE INSTANCES, MEASURED FROM THE DATASET ON THIS MACHINE -- not a guess, and not "
        f"something to re-derive: the graded split is `{m.group('task')}` at **n = {m.group('n')}**, "
        f"{count or m.group('size')} instances. " + cost +
        "Your speedup is the reference's total time over "
        "yours across all of them, so a constant overhead you would ignore at toy sizes is "
        f"{budget} of budget per instance here, and an asymptotically better algorithm "
        "that loses below n = 1 000 may still be the right answer. "
        "SIZE YOUR OWN CHECKS TO THAT n. A timing you take at n = 100 when the graded n is "
        f"{m.group('n')} does not measure this task."
    )
    if shape:
        clause += f" One instance has the shape: {shape}."
    return clause + " "


# ---------------------------------------------------------------- how the time is TAKEN
#
# THE MEASUREMENT PROCEDURE DECIDES WHICH OPTIMISATIONS EXIST, and the card never stated it. Checked
# against the goal the live probe actually ran on (`fullctx-probe/ws/algotune_convex_hull.json`,
# 9 221 characters): `warm`, `process`, `minimum`, `cache` and `memo` occur ZERO times in it between
# them, apart from one "warm start" about CP-SAT. A model reading that card has no way to know
# whether a JIT's first compile lands on its own clock, and the transcripts show it assuming the
# worst -- across arm B and the probes numba or Cython is raised 1 204 times and talked out of every
# time, while NINE of the seventeen published `convex_hull` champions compile something.
#
# EVERY SENTENCE BELOW WAS DRIVEN, not read: the arena's own `run_isolated_benchmark` was called on
# `AlgoTune/.venv/bin/python` (2026-08-27, cores 44-47, `num_runs=3`) against solvers built to
# expose one fact each.
#
#   * `solve()` sleeps 500 ms on its FIRST call and 10 ms afterwards
#         -> warmup 710.3 ms, timed 210.2 / 210.2 / 210.2 ms   (the first call is not the timed one)
#   * a 300 ms `time.sleep` at MODULE level
#         -> warmup 410.2 ms, timed 10.1 ms                    (import is outside both timers)
#   * a module-level `@njit` kernel first compiled from `__init__`
#         -> warmup 189.8 ms, timed 0.052 ms                   (the compile is absorbed)
#   * a `functools.lru_cache` and a module dict, both filled during the warm-up
#         -> the timed call sees `currsize=1, hits=0` and a dict of length 1
#            (the lru_cache was emptied, the dict was not)
#
# The code behind them, so the sentence and the harness can be checked against each other:
# `isolated_benchmark.py:838` warms up on `warmup_problem`, `:1011` times the call on
# `timed_problem`, `:909` empties the four cache attribute names, `:976-979` calls `cache_clear()`
# on every `functools` wrapper it finds, `:1585` reduces the runs with `min(timed_times_ns)`; and
# `evaluation_orchestrator.py:394` divides the baseline by `timing.min_ms`, so the MINIMUM is what
# the score is built from. The run count is `benchmark.runs` in the arena's config
# (`main.py:928` reads it into `RUNS`, `solver_executor.py:207` passes it as `num_runs`).
#
# DERIVED, both numbers, for the reason `rules_clause` is derived from the validator's tables: an
# arena retimed to measure once instead of three times, or one that renames the caches it clears,
# has to change this sentence rather than quietly make it false. No config, no claim.
#
# It rides with `--full-context` because it is the same KIND of statement as `dataset_clause` and
# `MEASURE` -- a measured fact about the ruler, not a rule about what the arena permits -- and
# because that flag is ON, so the correction ships. ADDING IT CHANGES THE GOAL CARD, i.e. changes
# the measurement: adopt it between arms, never inside one.
# WHY THE `__init__` HALF IS OUR OWN SENTENCE AND NOT THE ARENA'S. AlgoTuner's system prompt --
# `campaign-final/A-convex_hull.log:119`, repeated at `:459`, i.e. the first thing every arm-A run
# reads -- promises its agent that the time spent compiling in an init function is not charged to
# the solver's runtime. The sentence is quoted VERBATIM in
# `tests/test_algotune_timing_clause.py` and deliberately not here: this file is negatively pinned
# against it, and a commented-out copy would satisfy the substring the pin forbids.
#
# Copying that sentence across was the obvious move and it is WRONG in the general form it states.
# `isolated_benchmark.py:667` takes its callable from `solver_loader.get_fresh_solve_callable`,
# whose wrapper (`solver_loader.py:595-607`) does `SolverClass()` and then `.solve(problem)` --
# INSIDE the region `:1011` times. Driven on this box: a solver with a 200 ms `__init__` in front
# of a 10 ms `solve()` measures 210.2 / 210.2 / 210.2 ms. A constructor is charged in full, once
# per timed call.
#
# The arena's sentence is nonetheless true for the one case it names, by a mechanism it does not
# name: the WARM-UP call constructs the solver too, so a compile triggered from `__init__` is paid
# there and the dispatcher is warm by the timed call (189.8 ms warm-up, 0.052 ms timed). So the
# card states the MECHANISM instead of the promise -- the warm-up absorbs first-call costs, the
# constructor runs again on the clock -- which covers the compile case AND the case the arena's
# wording invites a model to get wrong: moving real per-call setup into `__init__` because a prompt
# said it was free.
_CACHE_ATTRS_RE = re.compile(r"for cache_attr in \[([^\]]+)\]")
_BENCH_RUNS_RE = re.compile(r"^benchmark:\s*$.*?^\s+runs:\s*(\d+)\s*$", re.MULTILINE | re.DOTALL)


def benchmark_runs(root: Path) -> int | None:
    """`benchmark.runs` from the arena's own config -- how many times each instance is timed.

    Read with a regex rather than a YAML parse so this generator keeps its stdlib-only imports; the
    key is one integer under one top-level block and the pattern is anchored on both.
    """
    try:
        text = (root / "AlgoTuner" / "config" / "config.yaml").read_text(encoding="utf-8")
    except OSError:
        return None
    m = _BENCH_RUNS_RE.search(text)
    if not m:
        return None
    runs = int(m.group(1))
    return runs if runs > 0 else None


def cleared_cache_attrs(root: Path) -> list[str]:
    """The Solver class attributes `isolated_benchmark` EMPTIES between the warm-up and the timed
    call, read out of the list literal it iterates. `[]` when that loop is gone or has moved."""
    try:
        text = (root / "AlgoTuner" / "utils" / "isolated_benchmark.py").read_text(encoding="utf-8")
    except OSError:
        return []
    m = _CACHE_ATTRS_RE.search(text)
    if not m:
        return []
    return [name for name in re.findall(r"[\"\']([^\"\']+)[\"\']", m.group(1)) if name]


def per_instance_cap(root: Path) -> tuple[float, float, float] | None:
    """The per-instance timeout, from the arena's own source rather than from memory.

    Derived, for the reason `session_budget_s` is derived: the card's whole claim here is a RATIO
    against the reference, and a hand-typed 10 keeps being quoted after the constant moves. Returns
    (TARGET_TIME_MULTIPLIER, WARMUP_MULTIPLIER, MIN_TIMEOUT_S), or None where this root holds no
    arena -- and then the card says nothing rather than inventing a ceiling.

    READ FROM THE FILES UNDER `root`, not imported. The first cut imported `AlgoTuner.…` and its own
    "no arena means no claim" test caught it: once any arena is in `sys.modules` the import succeeds
    for EVERY root, so a process that had loaded a different checkout would have quoted that other
    checkout's constants into this card. An import cannot answer a question about a path.

    Read 2026-08-31 from `AlgoTuner/utils/evaluator/runner.py`: in isolated mode (the mode this
    bench runs in) the subprocess budget is `(1 + WARMUP_MULTIPLIER) * oracle_s *
    TARGET_TIME_MULTIPLIER`, i.e. 60x the reference's single-run time for 6 runs -- 10x per run.
    """
    import re

    def const(rel: str, name: str) -> float | None:
        f = root / rel
        if not f.is_file():
            return None
        m = re.search(rf"^{name}\s*(?::\s*float\s*)?=\s*([0-9.]+)", f.read_text(errors="replace"),
                      re.MULTILINE)
        return float(m.group(1)) if m else None

    mult = const("AlgoTuner/utils/evaluator/runner.py", "TARGET_TIME_MULTIPLIER")
    warm = const("AlgoTuner/utils/timing_config.py", "WARMUP_MULTIPLIER")
    if mult is None or warm is None:
        return None
    # MIN_TIMEOUT_S is a function-local in runner.py, so it is stated, not read; it only ever RAISES
    # the ceiling for very fast references and cannot make the 10x claim wrong.
    return (mult, warm, 10.0)


def timing_clause(root: Path, unteachable: bool = True) -> str:
    """HOW the number is taken, derived from the arena's config and its benchmark module."""
    runs = benchmark_runs(root)
    if runs is None:
        return ""
    clause = (
        " HOW YOUR TIME IS TAKEN, and it decides which optimisations exist. Every instance is "
        "measured in a FRESH PROCESS which imports your module, calls `Solver().solve()` once on a "
        "DIFFERENT instance as an UNTIMED WARM-UP, and only then calls `Solver().solve()` on the "
        f"graded instance with the clock running. That process is rebuilt {runs} times per instance "
        f"and your time is the FASTEST of the {runs}, so one noisy run costs you nothing. "
        "What that buys you, each of these measured on this box by running the arena's own "
        "`run_isolated_benchmark` on a solver written to expose it: "
        "A FIRST-CALL COST IS PAID BY THE WARM-UP AND NEVER REACHES YOUR NUMBER -- import-time work, "
        "a JIT's first compile, a table you build lazily: the warm-up pays it and the timed call "
        "finds it done. Measured: a solver whose `@numba.njit` kernel compiles on first use warms up "
        "in 190 ms and TIMES AT 0.05 ms. "
        "`__init__` IS ON THE CLOCK, though. A fresh `Solver()` is constructed INSIDE the timed "
        "call, so everything the constructor does beyond that one-off compile is charged to every "
        "measurement. Measured: a 200 ms `__init__` in front of a 10 ms `solve()` is timed at "
        "210 ms. Setup belongs at module level or behind a first-call guard, where the warm-up "
        "absorbs it -- not in a constructor that runs again with the clock running. "
    )
    # GATED so the clause can be REMOVED for a control arm. It is one of the two rules a run
    # never demonstrates (see KEEP_BEST for the other), which is exactly why its effect is
    # unmeasurable without an arm that lacks it -- and until this gate existed there was no
    # way to build that arm at all. Default ON: the measuring arm is the shipped one.
    cap = per_instance_cap(root) if unteachable else None
    if cap:
        mult, warm, floor = cap
        clause += (
            "AND THERE IS A CEILING ON HOW SLOW YOUR SOLVER MAY BE, PER INSTANCE. The harness gives "
            f"each instance's subprocess `(1 + {warm:.0f}) * reference_time * {mult:.0f}` seconds -- "
            f"{warm:.0f} warm-up runs plus one timed run, each allowed {mult:.0f}x the reference -- "
            f"floored at {floor:.0f} s. Cross it and the instance is KILLED, and a killed instance is "
            "not a slow score, it is an INVALID one. So a solver that is correct but more than about "
            f"{mult:.0f}x slower than the reference on any single instance does not score badly; it "
            "fails. This is the one number in this card that experience cannot teach you: your own "
            "`eval_train` reports the run that survived, never the ceiling it survived under. "
        )
    attrs = cleared_cache_attrs(root)
    if attrs:
        named = ", ".join(f"`{a}`" for a in attrs)
        clause += (
            "SOME STATE IS WIPED BETWEEN THE TWO CALLS, AND NOT ALL OF IT: before the timed call the "
            f"harness empties any Solver class attribute named {named} and calls `cache_clear()` on "
            "every `functools` cache it can reach, and anything under another name survives. "
            "Measured: an `lru_cache` filled during the warm-up came back EMPTY, a module-level dict "
            "filled beside it came back FULL. That is not a way to precompute the answer -- the "
            "warm-up runs on a DIFFERENT instance and the graded one is solved once -- but work that "
            "does not depend on the instance can be done before the clock starts. "
        )
    return clause



# THE PRICE THE CARD QUOTED WAS NOT MEASURED, AND IT ARGUED AGAINST THE CAPABILITY IT WAS ATTACHED
# TO. The retired sentence said an evaluation runs from half a minute to six minutes, that one call
# could therefore be a third of the whole session, and that the model should measure once and never
# twice on the same code. It is quoted VERBATIM in `tests/test_algotune_eval_cost_clause.py` and
# deliberately not here: it is a negative pin, and a commented-out copy would satisfy the substring
# the pin forbids.
#
# MEASURED, 2026-08-27, over every `eval_train` call this harness has on disk -- the `run_dev_command`
# tool spans in `fullctx-probe*/runs/*/run/spans.jsonl` and `model-probes/*/runs/*/run/spans.jsonl`,
# 56 calls in 10 runs: 54 completed, median 39.6 s, min 28.5 s, max 79.9 s. The scorer's own
# `eval_seconds` from the same runs' `nodes/*/score.log` agrees (28.6-80 s per task). Against the
# 1200 s the Developer's session is bounded at, that is 3 %, not a third. The two calls that did NOT
# complete are the six-minute end of the old sentence: both hit the THEN-600 s cap, both returned
# `exit=-9` and `(no output)`, and the cap is 450 s now.
#
# WHY THE OVERSTATEMENT COSTS SCORE. AlgoTuner's agent is handed this same number automatically after
# every edit -- 57 evaluations on `convex_hull` alone (`grep -c '^Speedup: ' campaign-final/
# A-convex_hull.log`). Ours, in the ten runs that had the command at all, called it 1 to 13 times,
# median 4.5, and the card is what taught it to ration: a paragraph that says a measurement costs a
# third of the session and must never be repeated is a paragraph that says do not use this tool.
#
# What replaces it keeps the honest half -- the clock is real, a hung call returns nothing, and a
# session that writes no file has produced nothing -- and drops the arithmetic that was wrong. The
# numbers are stated as MEASURED CONSTANTS with their provenance above, because unlike the reference
# time and the instance shape there is nothing on this box to derive them from at build time: the
# cost of an evaluation is a property of runs that have not happened yet. The cap and the session
# budget ARE derivable and are derived.
#
# CHANGING THIS CHANGES THE MEASUREMENT. The probes running right now already have their cards built
# and are unaffected, but the next batch is comparable only with itself -- the same rule
# `rules_clause` states for its own additions: adopt between arms, never inside one.

# The `eval_train` ceiling, in ONE place: the card quotes it and the developer command is pinned at
# it, so the sentence the model reads and the timeout that kills it cannot drift apart. 450 s clears
# the slowest evaluation observed on this split (374.6 s) by 20 % and caps a hung call at 37 % of the
# session rather than the 50 % the model's own 600 s cap allowed.
DEV_EVAL_TIMEOUT_S = 450.0


def session_budget_s() -> float | None:
    """The Developer session's wall-clock ceiling, from the engine that enforces it.

    Read from `Settings` rather than typed, because the card's whole point here is the RATIO between
    one call and the session, and a hand-copied 1200 would keep being quoted after the default moved.
    None when this generator is run somewhere `looplab` is not importable -- and then the card states
    the seconds and skips the fraction, rather than inventing a denominator.
    """
    try:
        from looplab.core.config import Settings
    except Exception:                            # noqa: BLE001 - no engine here, no claim about it
        return None
    budget = float(getattr(Settings(), "developer_session_time_budget_s", 0.0) or 0.0)
    return budget if budget > 0 else None


def eval_cost_sentence() -> str:
    """What one `eval_train` call actually costs, and what follows from that."""
    budget = session_budget_s()
    share = (f", so one call is about {round(100 * 40.0 / budget):.0f} % of your session and not a "
             f"third of it" if budget else "")
    bounded = (f"against a Developer session bounded at {budget:.0f} s of wall clock"
               if budget else "against a Developer session bounded by a wall clock nobody shows you")
    return (
        "IT COSTS ABOUT 40 SECONDS, AND IT IS CHARGED TO A CLOCK YOU CANNOT SEE. Measured over the "
        "54 completed `eval_train` calls this harness has on record on this box: 40 s median, 29 s "
        f"fastest, 80 s slowest, {bounded}{share}. MEASURING IS THE CHEAPEST INFORMATION HERE -- the "
        "agent this arm is compared against is handed the same number automatically after EVERY "
        "edit it makes, 57 times on one task, and that is the loop you are up against. So: write "
        "the solver FIRST -- the simplest correct one -- then measure, change ONE thing, measure "
        "again. Two things do get expensive, and both are measured rather than feared. A call is "
        f"KILLED at {DEV_EVAL_TIMEOUT_S:.0f} s and a killed call returns nothing at all, not a "
        "partial result (it has happened twice, under an older 600 s cap: `exit=-9`, `(no output)`, "
        "half a session gone each time). And the evaluation runs YOUR solver over every instance, so "
        "a solver much slower than the reference makes the call itself slower -- on one task the "
        "node whose solver came out at 0.06x took 62 s where the other 28 nodes took 39-40 s. "
        "Neither is a reason to measure less; both are reasons to keep the clock in mind and to end "
        "the session with a FILE. A session that spends its clock measuring and ends with no file "
        "written has produced nothing at all. "
    )


# The clause that REPLACES `_DELIVER_NO_MEASURE`. It states what is now true, and states the cost,
# because an expensive capability offered without its price is used until the budget is gone.
MEASURE = (
    " YOU CAN MEASURE YOUR OWN SCORE, AND YOU SHOULD -- ON THE TRAIN SPLIT, THE SAME ONE EVERY NODE "
    "IS SCORED ON. `run_dev_command(\"eval_train\")` runs the REAL evaluator over the real "
    "instances with your currently staged files and prints the same JSON the scorer prints: "
    "`speedup`, `eval_seconds`, and whether every instance was valid. That is the number, not an "
    "estimate of it. Use it the way you would use a test: write the simplest correct solver, "
    "measure, change ONE thing, measure again. "
    "{cost}"
    "A guess you can check in one command is not a guess to write into the summary. "
    # THE SECOND HALF OF THE SAME REPAIR, and it is cheap where the first is expensive.
    #
    # `eval_train` says THAT the solver is slow. It cannot say WHERE, and the arena's agent has
    # never had to guess: `profile` and `profile_lines` are in its system prompt
    # (`campaign-final/A-convex_hull.log:181`) and it calls them 58-194 times per task. Measured
    # 2026-08-27: `line_profiler` 5.0.2 is installed in the very venv our scores are computed in.
    # The price is stated here for the same reason `eval_train`'s is -- both were measured, not
    # estimated: `convex_hull`, whole command, three solves plus dataset load, 1.06 s wall.
    " YOU CAN ALSO SEE WHERE THE TIME GOES. `run_dev_command(\"profile\")` runs your staged "
    "`solver.py` on ONE REAL train instance -- the full graded size, not a toy -- under a "
    "line profiler, and prints the hottest lines of your own code with hits, milliseconds and "
    "share of the run. It instruments every function in the files you wrote, so a helper's inner "
    "loop is named, not hidden behind the line that called it. IT IS CHEAP: a fixed ~1.3 s plus "
    "three runs of your own solve() (cold, warm, profiled) on that one instance -- against the "
    "tens of seconds of `eval_train`, which runs it over a hundred. So profile freely and "
    "measure rarely. It profiles ONE instance and reports no speedup and no validity -- it tells "
    "you where your time is, never whether you are winning. "
    # THE THIRD MEASUREMENT, and the only one that answers a question the model INVENTS.
    #
    # The arena's agent has two commands for this -- `reference <input>` and `eval_input <input>`
    # (`campaign-final/A-integer_factorization.log`, the system prompt's command list) -- and over
    # arm A's twenty task-arms it used them 119 and 97 times, against 177 `eval` calls. They are its
    # two most-used diagnostics after the score itself, and the transcript shows what for:
    # `reference {"composite": 15}` issued verbatim "to understand the problem sizes".
    #
    # We cannot copy the SHAPE: `run_dev_command(name)` takes a name and no arguments on purpose,
    # so the operator's argv cannot be forged, and a `--input` flag on the checker would be a door
    # the model has no way to open. But the CAPABILITY is already here and always was -- the
    # reference module sits in the staged tree, and `run_probe` runs Python over that tree.
    #
    # It is the AFFORDANCE that was missing, and the corpus says so: 3,124 probes, of which 95
    # (3.0 %) import the reference at all and 72 (2.3 %) call `is_solution` or `generate_problem`.
    # The models were writing timing loops by hand next to a module that answers the question.
    " AND YOU CAN ASK THE REFERENCE ANYTHING, ON AN INPUT YOU MAKE UP. `reference_<task>.py` is a "
    "real module sitting in your staged tree, so a `run_probe` can import it and call it: "
    "`generate_problem(n, random_seed=k)` builds an instance, `solve(problem)` returns the "
    "reference's own answer, and `is_solution(problem, answer)` says whether YOUR answer would be "
    "accepted and logs the reason when it would not. That is how you learn the input's SHAPE and "
    "SCALE, what edge cases the reference refuses, and whether a faster-but-different formula is "
    "still acceptable -- none of which `eval_train` or `profile` will ever tell you. It costs a "
    "probe, it writes nothing, and it touches no baseline. "
    "THE REPORTED SCORE IS ON A SPLIT YOU CANNOT SEE. Train is what you tune against; the champion "
    "is finally scored on held-out instances from the same generator. So anything that fits the "
    "train instances SPECIFICALLY -- a lookup table, a hard-coded answer, a threshold tuned to one "
    "of them -- scores zero where it counts. Make it fast for instances of THIS SHAPE, not for "
    "these hundred. "
)


# --one-card: the whole run is ONE hypothesis, implemented ONCE, and the ONLY way to test anything
# is to submit code and read the evaluation.
#
# Measured on this box (2026-08-21, budget $0.15/task, `convex_hull` + `discrete_log`): the run never
# reaches an evaluated node. Where the money goes, by phase: propose 44%, stages 28%, plan 21%,
# card_build (the Developer's only turn) 1%. So 93% is spent BEFORE any code is written, and the
# node that does get created is never evaluated.
#
# What propose spends it on, call by call: the workspace is THREE files totalling 20 KB, and the
# Researcher made 30 tool calls in it. Seven of those pulled ONE 400-line file through a paginated
# reader, out of order. Six went to stores the same turn had already told it were empty -- the
# published inventory in its own user message reads `find_concept_slugs=0 data_schema=0
# cross_run_prior_attempts=0`, and it called them anyway. Then the entire sequence replayed
# byte-for-byte (served from the gateway's cache at 0.0 s per call, and charged in full: 13 of 63
# calls, 22% of the task's budget).
#
# `--role-split` was written for the layer below this one -- the Developer choosing an algorithm by
# probing -- and was measured on the same two tasks at the same budget: same outcome, node created,
# never evaluated. So the constraint has to move UP, to how many things the run is allowed to be
# about, and OUT, to what counts as a test.
#
# Three rules, and each names the behaviour it forbids rather than describing a virtue (a fourth,
# numbered 0 because it happens before them, was added later -- see the note below it):
#   1. ONE CARD. Think about as many directions as you like; commit to exactly one.
#   2. TESTING IS THE EVALUATION. Not the probe, not a benchmark the agent writes for itself.
#   3. THE DEVELOPER IMPLEMENTS, and that is all it does.
# The (0) clause was added after the arm above ran, and it is a different KIND of rule from (1)-(3):
# those name a behaviour to stop, this one corrects a misreading of (1) itself -- "commit to exactly
# one idea" turned reading the file into one of the things you could commit to. Under `--one-card`
# a card is the scarcest thing in the run -- a $1.00 task produces three or four -- and the
# Researcher spent the first one describing how it intended to start.
# Measured 2026-08-21 (`/var/tmp/looplab-bench/runs-armb`, the `--deliver --one-card
# --enforce-rules` arm): the first hypothesis of `max_independent_set_cpsat` was "PREREQUISITE
# (must happen before any experiment): read reference_max_independent_set_cpsat.py to extract the
# exact contract"; of `queens_with_obstacles`, "FIRST ACTION (blocking): Have the Developer read
# reference_queens_with_obstacles.py and record the exact contract ... This is the single
# highest-value step and cannot be done with the available tools." On `convex_hull`, `kcenters` and
# `pagerank` the same shape reached card-0 itself. Across the 20-task run, 22 of 129 hypotheses and
# 3 of 58 cards were procedure rather than experiment.
#
# The cause is visible in the phase spans and it is not laziness: in `deep_research` -- the phase
# that mints those first hypotheses -- the Researcher made 17 (MIS) and 14 (queens) tool calls and
# NOT ONE of them opened `reference_*.py` or `description.txt`; every call went to the memory and
# concept stores. It then proposed, correctly, that somebody should read the file. The reading did
# happen later (18 `repo_read`s of the reference in `propose`, 6 more in `plan`), so the file was
# always reachable -- it was simply never treated as something you do BEFORE having an idea.
#
# So the clause states a fact about cost ("it costs no card") and then names the four spellings of
# the non-experiment, because the shape recurs under new names. It deliberately does NOT say "read
# widely" or "investigate the environment": the read fence and the grader fence exist because an
# earlier run made 119 probe calls, 116 of them reading the grader. The last sentence closes that
# door in the same breath as opening this one -- two files, and the harness is not one of them.
# WHICH NODE IS SUBMITTED, and it is not the last one. The other rule experience cannot teach.
#
# The champion is chosen among nodes that are `evaluated`, `feasible` and carry a metric -- an
# unevaluated node cannot be it, however promising the summary. Nothing in a run tells the model
# this: it sees its own nodes in order and has every reason to assume the newest is the answer.
#
# Measured 2026-08-30 (`remDL2`, discrete_log): node_0 scored 14.29 and node_1 scored 13.98, and
# the run submitted node_0. The first direct evidence that the rule bites rather than merely
# holding -- until that run every probe's last node also happened to be its best, so the rule was
# invisible in the corpus. `remPde` the same day spent 74 % of its dollar before ANY node existed,
# which is the same rule seen from the other end: nodes that are never evaluated cannot be kept.
#
# The consequence for the model is what the clause states: a risky rewrite late in the run cannot
# LOSE anything already measured, so the timid move -- leaving a good solver alone and running out
# the clock -- buys nothing it did not already have.
KEEP_BEST = (
    " THE BEST EVALUATED SOLVER IS WHAT GETS SUBMITTED, NOT YOUR LAST ONE. Every version you "
    "evaluate is kept and scored; at the end this run submits the highest-scoring one, and a "
    "version you never evaluated cannot be submitted at all. Two things follow, and they point the "
    "same way. A change that turns out WORSE costs you nothing -- the better version is still "
    "there, still the one that counts -- so there is no reason to protect a working solver by "
    "leaving it alone. And a change you never got around to EVALUATING is worth exactly zero, "
    "however good the reasoning behind it. Measure early, measure often, and spend late time on "
    "attempts rather than on caution. "
)


ONE_CARD = (
    " HOW THIS RUN IS ORGANISED, and it is not negotiable:\n"
    "(0) READING THE TWO FILES YOU WERE GIVEN IS A PRECONDITION, NOT AN EXPERIMENT, AND IT COSTS "
    "NO CARD. `description.txt` and `reference_{task}.py` are already in this workspace. The "
    "contract they state -- what `problem` holds, what `solve()` must return, what `is_solution()` "
    "actually checks and what it does NOT check -- is the only thing in this run that is KNOWN "
    "rather than guessed, so read them once, end to end, BEFORE you commit an idea, and carry what "
    "you learned INTO the idea as a stated fact. What does not count as a hypothesis, in any "
    "wording: \"first read the reference\", \"extract the exact contract\", \"establish a "
    "correctness baseline harness\", \"land a faithful port so the contract becomes readable\". "
    "None of those names a change to solver.py, and every one of them spends a turn that could have "
    "been an experiment. Those two files are also the WHOLE of the reading: the evaluator and the timer are "
    "fenced and are not yours to look at. `reference_{task}.py` DOES contain this task's instance "
    "generator as well as its `solve()` and `is_solution()`, and you may read all of it -- it is "
    "the file you were handed. What you may not do is write a solver that recognises the generator "
    "output instead of solving the problem: the arena's validator refuses that, and it would not "
    "be a result.\n"
    "(1) ONE HYPOTHESIS. The Researcher may consider any number of directions in its own reasoning, "
    "but it ends its turn having committed to EXACTLY ONE concrete idea, and the run works that one "
    "idea. Not a menu, not \"A, and if that fails B\", not a decision tree with an unresolved branch "
    "condition. If the idea rests on something nobody here knows yet, STATE THE ASSUMPTION inside the "
    "idea and commit anyway -- an assumption that turns out wrong is a finished experiment with a "
    "result, which is what this loop reads and builds on.\n"
    "(2) THE SCORE COMES FROM THE SUBMITTED CODE, AND ONLY FROM THERE. The evaluation is run on the "
    "file you submit, against instances that are not on this machine, and its report is the only "
    "evidence about SPEED that exists. So a timing you take here measures your guess about the "
    "input, not the score -- but if you want to check that something imports, that an API returns "
    "the shape you expect, or that your answer is CORRECT on a case you built, do it: correctness "
    "you can establish locally and speed you cannot. Spend the run on submissions, because that is "
    "what the loop reads and builds on.\n"
    "(3) THE DEVELOPER IMPLEMENTS THE IDEA IT WAS GIVEN. Mechanically, once, exactly as named. It "
    "does not pick between algorithms, does not benchmark alternatives, does not redesign the "
    "approach, and does not use any tool to decide WHAT to write -- only, at most, to make the named "
    "thing actually RUN (does this import, what shape does this API return). If the Developer "
    "believes the idea is wrong, it implements it anyway and says so in its rationale; a different "
    "idea belongs to the NEXT experiment, not to this one.\n"
    "Getting a submitted, evaluated solver -- even a mediocre one -- is worth more than any amount of "
    "investigation that produces none."
)


def rules_clause(root: Path) -> str:
    """The arena's submission rules, in the goal, DERIVED from the validator that enforces them.

    Not hand-written prose: a copied ban list goes stale in the direction that silently permits, and
    the operator then reads a goal that promises a rule the arena no longer has (or, worse, does not
    promise one it does). This reads `AlgoTuner.security.code_validator`'s own tables, so the
    sentence and the check cannot disagree.

    IT HAD ALREADY GONE STALE IN EXACTLY THAT DIRECTION, and the sentence above was the reason
    nobody looked. Only ONE of the validator's tables was read (`PROTECTED_MODULES`); the other
    prohibitions were typed out, and `DISALLOWED_CALLS` -- `exec`, `eval`, `compile`,
    `gc.get_objects` -- was not stated in any wording at all. Verified 2026-08-23 by running the
    real `check_code_for_tampering` on a four-line solver that compiles a specialised expression:
    `Error: Code contains security violations ... Calling compile is not allowed in solver code`.
    A run under `--enforce-rules` turns that into `looplab_failure_reason: rules_violation`, which
    the engine ends the node on and spends NO repair on -- so runtime code generation, a real
    optimisation family for a task that is scored on speed, costs one of the three or four
    experiments a $1.00 run gets, having been promised a complete rule list. Checked against the
    goal actually shipped to the live campaign (`ws-B/algotune_spectral_clustering.json`): the
    words exec/eval/compile appear in it ten times and every one of them is "evaluator".

    So `DISALLOWED_CALLS` is derived too. What CANNOT be derived is the rest: the ctypes import, the
    `sys.modules` access and the `is_solution` override are AST rules with no table behind them, so
    they stay prose -- and `tests/test_algotune_goal_clauses.py` pins each stated prohibition by
    RUNNING the real validator on a snippet that breaks it, which is the only check that cannot
    drift. Adding to this clause CHANGES THE GOAL CARD, i.e. changes the measurement: adopt it
    between arms, never inside one.

    Why state them at all when AlgoTune does NOT — its two prompt files mention no ban anywhere, and
    its agent learns the rule only by having an edit refused. Because the shapes differ: their agent
    edits and is refused in the same turn, at no cost. Ours has a RESEARCHER that commits to an
    algorithm before any code exists, and an idea resting on a forbidden primitive burns a whole node
    before anyone finds out. The cheapest place to say it is therefore before the idea, not after the
    edit.
    """
    try:
        sys.path.insert(0, str(root))
        from AlgoTuner.security.code_validator import TamperingDetector
    except Exception:                           # noqa: BLE001 — no validator, no claim about rules
        return ""
    finally:
        if sys.path and sys.path[0] == str(root):
            sys.path.pop(0)
    protected = sorted(getattr(TamperingDetector, "PROTECTED_MODULES", ()) or ())
    if not protected:
        return ""
    # The second table, and the one the goal used to be silent about. Sorted so the sentence is
    # stable across runs; `gc.get_objects` sits in here beside the three builtins because the
    # validator matches it by the same dotted-name rule.
    disallowed = sorted(getattr(TamperingDetector, "DISALLOWED_CALLS", ()) or ())
    # THE GLOSS USED TO SAY THE OPPOSITE OF WHAT THE VALIDATOR DOES, and it cost more than any
    # missing rule ever did. It read: "the arena refuses runtime code generation ... so an idea that
    # turns on generating and compiling specialised code is not one it can accept, however fast the
    # result would be." The table behind it is `DISALLOWED_CALLS`, and it holds BUILTIN CALLS —
    # `compile`, `eval`, `exec`, `gc.get_objects`. It says nothing about a JIT.
    #
    # RUN AGAINST THE REAL `check_code_for_tampering` 2026-08-27:
    #     a solver decorated `@njit(cache=True, fastmath=True)`   -> accepted, no violation
    #     a `setup.py` calling `cythonize("fast.pyx")`            -> accepted, no violation
    #     `compile("x+1", "<s>", "eval")` then `eval(...)`        -> "Calling compile is not
    #                                                                allowed in solver code"
    # And `numba` sits in this same validator's `PROTECTED_MODULES` beside numpy, scipy and ortools,
    # i.e. the arena lists it among the libraries a solver is expected to import.
    #
    # WHAT THE FALSE GLOSS BOUGHT: across arm B's twenty runs and the probes, the models raise numba
    # or Cython 1,204 times and talk themselves out of it every time — "might be considered runtime
    # codegen (forbidden: no compile/eval/exec)", "Cython/numba (not available, and arena
    # restricts)". Not one of our twenty champions compiles anything. Of the seventeen published
    # champions for `convex_hull`, NINE do, and they are the strongest ones.
    calls = (" No call to " + ", ".join(f"`{c}`" for c in disallowed) +
             " — those are BUILTINS, and the ban is on calling them: the arena refuses code the "
             "solver assembles from strings at run time, and refuses harness introspection. It is "
             "NOT a ban on compilation. A JIT or an ahead-of-time compiler is an ordinary library "
             "here: `@numba.njit` on a hot loop, or a `.pyx` with a `setup.py`, breaks no rule — "
             "numba is on the arena's own protected-library list, next to numpy and scipy."
             if disallowed else "")
    return (" ARENA RULES FOR THE SUBMITTED SOLVER, enforced by this benchmark's own validator "
            "before anything is scored — a solver that breaks one is not scored low, it is NOT "
            "SCORED: no `import ctypes` (directly or through `__import__`), no reading or writing "
            "`sys.modules`, no redefining `is_solution`, and no monkey-patching of the standard "
            # The tail is CONDITIONAL: `PROTECTED_MODULES` is a third-party list and a shorter one
            # made the goal every turn is built from say "and 0 more" -- or, at five entries,
            # "and -3 more". A goal card that visibly cannot count is not one a model should be
            # asked to take literally.
            "modules the arena protects (" + ", ".join(protected[:8]) +
            (f", and {len(protected) - 8} more" if len(protected) > 8 else "") +
            "). Note what that last one is and is not: the "
            "validator's rule is that you may not REASSIGN those modules' attributes, and most of "
            "the list is the scientific stack itself, so importing them and calling them normally "
            "is not merely allowed, it is the expected way to write this file. If an idea only "
            "works by reaching under the runtime for one of these, it is not an idea this arena "
            "can accept — pick a different one rather than a way around." + calls)


# --enforce-rules, second half: WHAT IS PERMITTED. `rules_clause` states only prohibitions, and a
# prohibition list read on its own reads narrower than the rule is -- the sentence this clause pair
# replaced told the solver to write plain algorithmic Python and nothing else, which on a task whose
# reference IS a solver-library call is an instruction to leave that library alone. (The retired
# sentence is quoted in `tests/test_algotune_goal_clauses.py`'s docstring and nowhere in this file:
# it is a negative pin, and a commented-out copy would satisfy the substring it forbids.)
#
# Measured 2026-08-21 across `/var/tmp/looplab-bench/runs-armb` (20 tasks, `--deliver --one-card
# --enforce-rules`): on the four tasks whose reference is CP-SAT and nothing else --
# `max_independent_set_cpsat`, `queens_with_obstacles`, `min_dominating_set`, `max_common_subgraph`
# -- EVERY hypothesis and EVERY card was a hand-written exact combinatorial search in pure Python
# (bitset branch-and-bound, greedy-colouring bounds, McSplit, Levi association graphs). Not one
# proposed calling `ortools` itself with a different model, different parameters or a warm start.
# Best scores: 0.2933, 0.3101, 0.3592, 0.3470 — and on `max_independent_set_cpsat` all three cards
# landed inside 0.2824-0.2933, three attempts at one family.
#
# It is not that the family loses. On the two tasks where somebody DID think of it,
# `multi_dim_knapsack` ("Seeding CP-SAT with a greedy warm-start hint", "num_search_workers=8 on a
# minimal model identical to the reference") and `rectanglepacking` ("keep the exact CP-SAT model
# but cut the time limit ... CP-SAT spends the remaining time proving optimality, which
# is_solution() does not require"), the cards were built, ran and scored 0.4950 and 0.3740 — the
# top of that whole cluster. The family was excluded by the goal's wording, not by the arena.
#
# DERIVED from the reference's own import statements, for the same reason `rules_clause` is derived
# from the validator's own tables: a hand-written "you may use OR-Tools" would be a per-task claim
# maintained by hand, would be wrong on the 16 tasks that use scipy/cvxpy/networkx instead, and
# would go stale silently. The reference imports it, the evaluator runs the reference, therefore
# the import resolves in the interpreter that will run solver.py — that is a fact, not advice.
#
# What it deliberately does NOT do is choose. It names the family and the generic levers (model,
# parameters, bound, warm start, stopping rule) and stops there; which lever, and whether to use
# the library at all, stays the Researcher's committed guess, because the point is to stop
# EXCLUDING a family, not to hand over an answer the evaluation is supposed to give.
def reference_libraries(ref: Path) -> list[str]:
    """Third-party top-level modules `ref` imports — stdlib and the harness's own packages removed.

    `AlgoTune*` is dropped because `from AlgoTuneTasks.base import Task` is the registration
    boilerplate every reference carries and is exactly what the candidate may NOT import; naming it
    here as an available library would contradict the fence two clauses up.
    """
    try:
        tree = ast.parse(ref.read_text(encoding="utf-8"))
    except Exception:                        # noqa: BLE001 — unparseable reference, no claim
        return []
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".", 1)[0])
    return sorted(r for r in roots
                  if r and r != "__future__" and not r.startswith("AlgoTune")
                  and r not in sys.stdlib_module_names)



def toolchain_clause(root: Path, interpreter: str) -> str:
    """What is actually importable in the interpreter that SCORES, with versions, measured now.

    The card named the data shape and the reference time and stopped there, and the models filled
    the silence with a guess that cost score. Across arm B's twenty runs and the probes they raise
    numba or Cython 1,204 times and reject it every time in one of two ways -- "Alternative: use
    numba? Not available probably", "Cython/numba (not available, and arena restricts)". Both are
    false. `AlgoTune/.venv` carries numba 0.67.0 and cython 3.2.9, and the arena's own validator
    accepts an `@njit` solver (checked by running `check_code_for_tampering` on one).

    The cost of the silence: not one of our twenty champions compiles anything, while NINE of the
    seventeen published champions for `convex_hull` do -- Claude Opus 4.5/4.6, GPT-5.2/5.4,
    Gemini 2.5/3.1 Pro, R1, GLM-4.5, and Gemini 3 Pro ships a 17-line `.pyx`.

    MEASURED, not listed from a requirements file: each name is imported by the SCORING interpreter
    and reported only if that import succeeds. A package that is in `requirements.txt` but broken in
    this venv would otherwise be advertised to the model as a way forward.

    AlgoTuner tells its own agent the same thing and goes further -- its prompt lists thirty
    packages and adds a paragraph promising that a `.pyx` edit triggers a build. Ours had neither
    sentence while its bridge already did the build.
    """
    names = ("numpy", "scipy", "numba", "cython", "torch", "networkx", "ortools", "sklearn",
             "sympy", "cvxpy", "numexpr", "bottleneck")
    probe = (
        "import importlib\n"
        "for n in %r:\n"
        "    try:\n"
        "        m = importlib.import_module(n)\n"
        "    except Exception:\n"
        "        continue\n"
        "    v = getattr(m, '__version__', '')\n"
        "    print(n + ('==' + str(v) if v else ''))\n" % (names,)
    )
    try:
        out = subprocess.run([interpreter, "-c", probe], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""                                # no interpreter, no claim about what it holds
    found = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
    if not found:
        return ""
    return (" THE TOOLCHAIN ON THIS BOX, imported just now by the very interpreter that will time "
            "your solver, so this is a measurement and not a package list: " + ", ".join(found) +
            ". A compiled inner loop is the single technique that most separates the published "
            "champions of this benchmark from everything else, and nothing here forbids it: "
            "`@numba.njit` is an ordinary decorator, and if you write a `.pyx` together with a "
            "`setup.py`, the harness runs `setup.py build_ext --inplace` over your whole submission "
            "before scoring it and tells you whether that build succeeded. "
            # SAID PLAINLY, because the omission cost real work. The bridge's default is to submit
            # EVERY file the node wrote (`looplab_eval.py --solver-file-only` is the opt-OUT and is
            # off), but the card only ever said "over your whole submission" and left the model to
            # infer it. Measured 2026-08-28 on dsN3 node 1: its solver.py embeds ~90 lines of Cython
            # source in a string and compiles it at run time, with the reason in its own comment --
            # "embedded so the build works even if the evaluation copies only solver.py into the run
            # directory". It hedged against a limitation that does not exist, and paid for the hedge
            # with a compile INSIDE the timed evaluation, which is the same fragility that took ds3
            # from 156.43 on train to 0.0 on test.
            "EVERY FILE YOU WRITE IS SUBMITTED, not just `solver.py`: a `.pyx`, a `setup.py`, a "
            "`.pxd`, a helper module -- they are all copied beside your solver and the build runs "
            "over the set. You do not need to embed sources in strings or compile anything at run "
            "time to smuggle them in, and you should not: a build inside the timed call is both "
            "slower and a way to fail scoring that an ahead-of-time one cannot. Reach for it when the "
            "cost is a Python-level loop over the instance; do not reach for it when the work is "
            "already inside a library call. "
            # THE TWO ARE NOT INTERCHANGEABLE, and the card used to imply they were by naming them
            # in one breath. Measured over this campaign's own champions, scored on test:
            #   edge_expansion  .pyx median 207.48 (n=3)  @njit 27.97 (n=6)  plain numpy 27.80 (n=5)
            #   kcenters                     — (n=0)      @njit 123.38 (n=2) plain numpy 34.25 (n=2)
            #   convex_hull                  — (n=0)      @njit 11.08 (n=3)  plain numpy 2.03 (n=1)
            # So on some tasks `@njit` is the whole win and on others it is worth nothing at all:
            # six edge_expansion champions stopped at njit and landed within noise of solvers that
            # compiled NOTHING, while the two that shipped a `.pyx` scored 207 and 286. A model that
            # reads "njit or Cython" as one option stops at the cheaper one and never learns which
            # case it is in. The clause does not TELL it which to use -- that would be a guess about
            # the task -- it tells it the test that distinguishes them, which it can run for free.
            "THE TWO ARE NOT EQUIVALENT, and which one wins is a property of the task rather than "
            "of the technique. Measured across this campaign's own champions: on one task the "
            "`@njit` runs landed within noise of solvers that compiled nothing (27.97 against "
            "27.80) while the two that shipped a real `.pyx` scored 207 and 286; on two other "
            "tasks `@njit` alone was worth 3.6x and 5.5x over plain numpy. So treat `@njit` as a "
            "MEASUREMENT, not a destination: apply it, measure, and if the number did not move, "
            "the next step is an ahead-of-time `.pyx` extension rather than more decorator "
            "tuning -- njit that fails to help usually means the loop was never the bottleneck "
            "Python was paying for, and only a real extension removes the rest.")


def solution_space_clause(ref: Path, task: str) -> str:
    """What the arena PERMITS, derived from the reference's imports (see the note above)."""
    libs = reference_libraries(ref)
    if not libs:                             # a pure-stdlib reference borrows nothing to name
        return ""
    named = ", ".join(f"`{lib}`" for lib in libs)
    return (" WHAT THE SOLUTION SPACE INCLUDES — said explicitly because the paragraph above lists "
            f"only prohibitions and is read as narrower than it is. `reference_{task}.py` imports "
            f"{named}, the evaluator runs that reference on this machine, so those same imports "
            "resolve in the interpreter that will run your solver.py. Reaching for the SAME "
            "library the reference reaches for is not a loophole, not cheating and not a "
            "degenerate answer — it is the same starting line. The reference is a straightforward "
            "use of it, so re-modelling the problem for that library, changing how it is "
            "configured, giving it a bound or a warm start it does not currently get, or stopping "
            "it at the point `is_solution()` would already accept the answer (READ what it checks "
            "before assuming that point exists), are all legitimate hypotheses — exactly as "
            "legitimate as replacing the library with code you write yourself, and neither side is "
            "the default. Nobody here can measure which "
            "wins; commit to the one you actually believe and let the evaluation say. (The single "
            f"import that stays forbidden is `reference_{task}.py` itself — the library, never the "
            "reference.)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--task", required=True)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--python", default=None,
                    help="Interpreter that has AlgoTune installed "
                         "(default: <algotune-root>/.venv/bin/python).")
    ap.add_argument("--timeout", type=int, default=7200, help="Eval timeout in seconds.")
    ap.add_argument("--enforce-rules", action="store_true",
                    help="Score through AlgoTune's OWN solver validator and state its rules in the "
                         "goal — BOTH halves: what it forbids (see rules_clause) and what it "
                         "therefore permits, including the reference's own libraries (see "
                         "solution_space_clause). OFF by default: they are this ARENA's rules, and "
                         "a LoopLab task that is not an AlgoTune arm must not inherit them.")
    ap.add_argument("--one-card", action="store_true",
                    help="Append the READING IS A PRECONDITION / ONE HYPOTHESIS / THE ONLY TEST "
                         "IS THE SUBMITTED CODE / THE DEVELOPER IMPLEMENTS clause (see ONE_CARD). "
                         "Composes with the others.")
    ap.add_argument("--deliver", action="store_true",
                    help="Append the YOU CANNOT MEASURE YOUR OWN SCORE clause (see DELIVER).")
    # ON BY DEFAULT since 2026-08-26. It was introduced off, on the principle that a change to the
    # goal card is a change to the measurement and belongs between arms rather than inside one --
    # which is still true, and is why `--no-full-context` exists and why the twenty arm-B numbers
    # taken without it are not comparable to numbers taken with it.
    #
    # What changed is the judgement of what the BASELINE is. This benchmark's reference agent is
    # shown the graded metric on the train split 17-61 times per task, plus `eval_input` 207-429
    # times and `profile` 58-194 times on real instances. Withholding the instance size and any way
    # to measure was not a neutral default -- it was a handicap we applied to one arm only, and the
    # loop's answer to it was to invent sizes: `convex_hull` is n = 267 021 and the probes that
    # chose its champion ran at n = 100, 1 000 and 10 000. The default now gives the agent what it
    # is legitimately allowed to know, and the OPT-OUT is the thing you reach for deliberately.
    ap.add_argument("--full-context", action=argparse.BooleanOptionalAction, default=True,
                    help="Give this arm what the ARENA gives its own agent: the measured instance "
                         "shape in the goal (see dataset_clause) and a pinned `eval_train` command "
                         "that runs the real evaluator on the staged files, replacing the "
                         "--deliver clause's YOU CANNOT MEASURE half with MEASURE. ON by default; "
                         "`--no-full-context` reproduces the goal card the 2026-08-24 arm B ran on.")
    ap.add_argument("--unteachable-rules", action=argparse.BooleanOptionalAction, default=True,
                    help="State the two rules the run never demonstrates: the per-instance "
                         "ceiling (inside --full-context's timing clause) and KEEP_BEST "
                         "(inside --one-card). ON by default. `--no-unteachable-rules` is the "
                         "CONTROL arm for those two clauses and nothing else -- every other "
                         "clause the same flags produce is byte-identical, which is what "
                         "makes the two arms comparable.")
    ap.add_argument("--role-split", action="store_true",
                    help="Append the ONE EXPERIMENT = ONE ALGORITHM clause (see ROLE_SPLIT).")
    args = ap.parse_args()

    root: Path = args.algotune_root.resolve()
    task_src = root / "AlgoTuneTasks" / args.task
    if not task_src.is_dir():
        raise SystemExit(f"no such AlgoTune task: {task_src}")

    desc = task_src / "description.txt"
    ref = task_src / f"{args.task}.py"
    for required in (desc, ref):
        if not required.exists():
            raise SystemExit(f"task {args.task!r} is missing {required.name}")

    ws: Path = (args.out_dir / args.task).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    shutil.copy2(desc, ws / "description.txt")
    ref_name = f"reference_{args.task}.py"
    shutil.copy2(ref, ws / ref_name)

    solver = ws / "solver.py"
    if not solver.exists():          # never clobber work already in progress
        solver.write_text(SOLVER_STUB.format(task=args.task), encoding="utf-8")

    interpreter = args.python or str(root / ".venv" / "bin" / "python")
    # Read once, here, so the failure to read it is visible at build time rather than as a
    # silently-too-small check inside a run.
    _graded_n = graded_instance_size(root, args.task)
    train_path = train_dataset(root, args.task) if args.full_context else None
    if args.full_context and train_path is None:
        # REFUSE when it was ASKED FOR, WARN when it is merely the default. A flag that degrades
        # without saying so is how an arm ends up mislabelled -- but now that the default is ON,
        # hard-failing would also break every task whose split is not on this machine, which is a
        # different and worse failure. `--full-context` explicitly on the command line still means
        # "I require this", and the exit code still says so.
        asked = any(a == "--full-context" for a in sys.argv[1:])
        message = (f"--full-context: no train split found for {args.task!r} under "
                   f"{root / '.hf_datasets'}")
        if asked:
            raise SystemExit(message + " -- refusing to build a task that claims context it does "
                                       "not have")
        print(message + " -- building WITHOUT it (pass --full-context to make this fatal)",
              file=sys.stderr)
        args.full_context = False

    spec = {
        "kind": "repo",
        "id": f"algotune_{args.task}",
        "goal": (GOAL.format(task=args.task)
                 + (ROLE_SPLIT.format(
                       unknowns=(_UNKNOWNS_FULL_CONTEXT if args.full_context
                                 else _UNKNOWNS_DEFAULT)) if args.role_split else "")
                 # --full-context swaps the FALSE half of --deliver for the measured facts and the
                 # capability that makes them checkable. The half that says "write the file early,
                 # a working solver beats an unwritten plan" is true either way and is kept.
                 + (dataset_clause(root, args.task) if args.full_context else "")
                 # WHAT the instances are, then HOW they are timed -- the second is what decides
                 # whether the first one's per-instance overhead can be paid before the clock
                 # starts. See `timing_clause`.
                 + (timing_clause(root, args.unteachable_rules) if args.full_context else "")
                 + ((_DELIVER_WRITE if args.full_context else DELIVER) if args.deliver else "")
                 + (MEASURE.format(cost=eval_cost_sentence()) if args.full_context else "")
                 + (ONE_CARD.format(task=args.task) if args.one_card else "")
                 # Under the same flag as ONE_CARD: both are statements about how THIS loop scores
                 # what you produce, and both are rules the run itself never demonstrates. See
                 # KEEP_BEST -- it is the reason "commit to one card" is not a reason to be timid.
                 + (KEEP_BEST if args.one_card and args.unteachable_rules else "")
                 # BANS then PERMISSIONS, in that order and both under the same flag: they are one
                 # statement of what this arena allows, and the half that was missing is the half
                 # that costs score. See `solution_space_clause`.
                 + (rules_clause(root) if args.enforce_rules else "")
                 + (solution_space_clause(ref, args.task) if args.enforce_rules else "")
                 # The third half of the same statement: after what is banned and what the solution
                 # space is, WHAT THIS MACHINE ACTUALLY HOLDS. Same flag as the other two because it
                 # is the same subject -- what this arena allows -- and adopting it mid-arm would
                 # change the measurement.
                 + (toolchain_clause(root, interpreter) if args.enforce_rules else "")),
        "direction": "max",
        "editable_path": str(ws),
        # THE SAME SURFACE THE OTHER ARM HAS. `["solver.py"]` was our pin, not the arena's: arm A
        # edits a DIRECTORY — arbitrary files, `.pyx` compiled by `setup.py build_ext --inplace`,
        # Pythran, DaCe — and on AlgoTune those compiled paths are a primary source of the large
        # speedups in the published table. One arm could reach the technique that wins this
        # benchmark and the other could not, entirely because of this line.
        #
        # `protect` still holds: the two files the operator planted are read-only, and
        # `looplab_eval.py` refuses to submit them even if they change.
        "edit_surface": ["solver.py", "*.py", "*.pyx", "*.pxd", "setup.py", "pyproject.toml"],
        "protect": [ref_name, "description.txt"],
        # NO DATA MOUNT, and that is a measured decision rather than caution.
        #
        # Mounting the train split was the obvious move and both halves of it fail. (a) PARITY: the
        # reference agent never reads these files either -- it has `eval_input` (run on an input it
        # supplies) and `eval` (score on train), and no path to the dataset. Handing ours the
        # instances would be MORE than parity, in the one direction that matters, since the champion
        # is graded on held-out instances from the same generator. (b) COST: four of the twenty tasks
        # store their arrays outside the jsonl as `ndarray_ref` -> `_npy_data/<uuid>.npy`, and that
        # directory holds BOTH splits' arrays -- 200 files, 816 MB on `convex_hull` alone. Mounting
        # the directory leaks test; materialising only the train half is ~408 MB per node, and this
        # loop evaluates nodes concurrently.
        #
        # What the agent actually lacked was the SHAPE and a way to MEASURE. It now has both.
        # THE ARENA'S `eval`, which is the capability the comparison was missing. It runs the SAME
        # bridge the scorer runs, on the SAME train split, against the Developer's staged files --
        # so the number it prints is the number, not a proxy for it. The bridge removes its own
        # `results/<model>-<pid>/` and `reports/evaluate_summary.<pid>.json` on the way out, and
        # takes the shared baseline cache by default, so an invocation neither litters the
        # third-party checkout nor re-times the reference against itself.
        # THE SIZE THE ARENA GRADES AT, read from its own dataset filename rather than assumed.
        **({"developer_commands": [{
            "name": "eval_train",
            "command": [
                interpreter, str(BRIDGE),
                "--algotune-root", str(root),
                "--task", args.task,
                "--model", "DevEvalTrain",
                "--solver", "solver.py",
                "--subset", "train",
            ] + (["--enforce-rules"] if args.enforce_rules else []),
            "description": ("Run the REAL evaluator on the train split with your staged files and "
                            "print its JSON: speedup, eval_seconds, and whether every instance was "
                            "valid. This is the graded metric on the split nodes are scored on. It "
                            "costs tens of seconds to several minutes on the machine your solver is "
                            "timed on."),
            "cwd": ".",
            # THE RULER, PINNED INTO THE COMMAND, because the sandbox does not carry it.
            #
            # Caught on the first real invocation, 2026-08-26 07:53: the dev command ran without
            # `ALGOTUNE_EVAL_WORKERS`, so the bridge took the SERIAL path and keyed its baseline
            # `convex_hull__train__lane22r3` -- while the campaign's own entry, already on disk,
            # is `convex_hull__train__w22x1r3`. It therefore MISSED a warm cache and re-timed the
            # reference IN THE SAME PASS: 44 cache entries became 45, and the bridge's own guard
            # answers that with `speedup: null` + `baseline_measured_in_pass`. So the capability
            # both cost ~200 s more than it needed to AND could not return a number.
            #
            # Read from THIS process's environment at build time, which is the campaign's own
            # value, so the command measures on exactly the ruler the scorer stage measures on --
            # the whole point of offering it. Absent keys are simply not pinned: a task built
            # outside a campaign has no ruler to inherit and must not be given a fabricated one.
            "env": {k: os.environ[k] for k in (
                "ALGOTUNE_EVAL_WORKERS", "ALGOTUNE_BASELINE_CACHE_DIR", "ALGOTUNE_MIN_TIMEOUT_S",
            ) if os.environ.get(k)},
            # 450 s, AND THE NUMBER IS THE SESSION'S, NOT THIS COMMAND'S.
            #
            # 600 s was the model's cap (`DeveloperCommandSpec` refuses more) and it fit the work:
            # arm B's twenty task-arms scored their nodes on this same train split in 29.7 to
            # 374.6 s against a warm cache. What it did NOT fit is the clock it is spent against.
            # The Developer's session has a 1200 s time budget it cannot see, so one call that runs
            # to the cap costs HALF the session -- and measured on both probe attempts, 2026-08-26,
            # that is exactly what happened: a single `run_dev_command` of 600 s returning
            # `exit=-9` and `(no output)`, 795 s and 730 s of the 1200 s gone into tools, and NEITHER
            # attempt wrote a file. Zero nodes in ninety minutes, against 29 minutes to first node
            # for the same task without this command.
            #
            # 450 s clears the slowest observed real evaluation by 20 % and caps a hung one at 37 %
            # of the session instead of 50 %. It does not make the tool safe -- two bad calls still
            # end a session -- and the real repair is telling the Developer what its clock is,
            # which is a change to the session contract rather than to this task.
            "timeout": DEV_EVAL_TIMEOUT_S,
        }, {
            "name": "profile",
            "command": [
                interpreter, str(PROFILER),
                "--algotune-root", str(root),
                "--task", args.task,
                "--solver", "solver.py",
                "--subset", "train",
            ],
            "description": ("Line-by-line profile of your staged solver.py on ONE real train "
                            "instance at the full graded size. Prints the hottest lines of your "
                            "own code -- hits, milliseconds, share of the run -- across every "
                            "function in the files you wrote. Costs a fixed ~1.3 s plus three "
                            "runs of your solve() on that one instance. It reports no speedup and "
                            "no validity: use eval_train for those."),
            "cwd": ".",
            # NO RULER TO PIN. `eval_train` inherits ALGOTUNE_EVAL_WORKERS and the baseline cache
            # because it computes a RATIO against the reference and a ratio is meaningless off the
            # ruler it was taken on. This command computes no ratio: it times one solve against
            # itself and reports shares of it. The baseline cache is never touched, so an
            # invocation cannot warm, cool or corrupt the denominator the scorer uses -- which is
            # the whole reason it is safe to offer as the cheap one.
            "env": {},
            # 150 s, DERIVED FROM THE SCRIPT'S OWN CEILING RATHER THAN CHOSEN.
            #
            # The script gives each of its three solves (cold, warm, profiled) 45 s and prints a
            # PARTIAL profile on expiry, so its own worst case is 3 x 45 s plus setup. Setup was
            # measured 2026-08-27 across all twenty campaign tasks: instance load is 0.03-0.45 s
            # (convex_hull, the 816 MB one, is 0.45 s because the arrays are memmapped) and the
            # whole command on convex_hull is 1.06 s wall. A deliberately non-terminating solver
            # with `--solve-timeout 3` took 10.23 s = 3 x 3 s + 1.2 s, which fixes the overhead at
            # ~1.3 s; 3 x 45 + 1.3 = 136.3 s, and 150 s clears that by 10 %.
            #
            # The number is small ON PURPOSE and the session clock is why. `eval_train` is 450 s
            # against a 1200 s session the Developer cannot see, so two bad calls end it. This one
            # is 12.5 % of the session at its ceiling and ~0.1 % in practice, which is what makes
            # "profile freely, measure rarely" advice the model can actually follow.
            "timeout": 150.0,
        }, {
            "name": "check",
            "command": [
                interpreter, str(CHECKER),
                "--reference", f"reference_{args.task}.py",
                "--solver", "solver.py",
                "--n", "3",
            ] + (["--size", str(_graded_n)] if _graded_n else []),
            "description": ("Run your staged solver on 3 FRESH instances and ask the reference's "
                            "own is_solution() whether each answer is acceptable -- printing the "
                            "rejection reason verbatim when it is not. Seconds, not minutes. It "
                            "reports NO speedup: it answers `would this score at all`, which "
                            "eval_train answers only after tens of seconds. Use it after every "
                            "change that could alter what your solver RETURNS, and especially "
                            "before trading exactness for speed."),
            "cwd": ".",
            # WHY THIS EXISTS, measured 2026-08-28 over three independent kcenters probes. Of the
            # writing sessions' own `run_probe` self-checks: dsKcCtl 55 probes, of which 3 compared
            # against the reference solve, 1 called is_solution, and 23 timed something; dsFBKc
            # 42/3/1/13; fxKcenters 55/3/4/28. The sessions were checking SPEED, and the score is
            # zero unless every instance validates. dsKcCtl's node 1 is the bill: it knowingly
            # traded exactness for speed, self-checked 55 times, and the ENGINE was the first thing
            # to say `Solution is not optimal. Found value: 33.955, Optimal value: 33.408` -- 117.9 s
            # of evaluation to learn it, the node scored 0.0, and its work was discarded. Run
            # against that same solver, this command reports the invalid instance in 3.7 s with the
            # reason attached; against node 0's valid champion it reports all-valid.
            #
            # The reference agent has had this the whole time and it is not an instruction there but
            # a COMMAND -- `reference [1,2,3]` and `eval_input [1,2,3]` -- so its model never writes
            # a validation harness by hand. Ours was handed a blank `run_probe` and wrote timing
            # loops with it.
            #
            # NO RULER PINNED, deliberately, exactly as `profile`. This computes no ratio and never
            # touches the baseline cache, so it cannot warm, cool or corrupt the denominator the
            # scorer divides by. It also generates its OWN instances rather than reading the graded
            # split, so it cannot be mistaken for -- or quietly become -- the score.
            "env": {},
            # 120 s. The measured cost is dominated by the FIRST instance (import plus any JIT):
            # 3.66 s on dsKcCtl node 1 and 2.56 s on node 0, with every later instance at 0.0002 s.
            # A ceiling of 120 s therefore clears a normal call by ~30x and still caps a
            # pathological one at 10 % of the 1200 s session -- cheaper than `profile`'s 150 s,
            # which is the point: this is the one the model should reach for constantly.
            "timeout": 120.0,
        }]} if args.full_context else {}),
        "eval": {
            # DECLARED AS A ONE-STAGE PIPELINE, not as a bare `command`, and the difference is not
            # cosmetic. `repo_developer.py::_operator_stage_list` reads `eval.stages`; when it is
            # present and valid the engine runs it verbatim, the Developer's own manifest is
            # IGNORED, and its `stages` phase is SKIPPED entirely.
            #
            # With only a `command` that phase runs, and its prompt is written for ML pipelines:
            # "declare the ordered stages that run BEFORE it ... GOOD PRACTICE: separate stages for
            # data/feature PREPARATION, TRAINING (a fresh model every node) and TESTING; bake this
            # node's hyperparameters into the `train` command". An AlgoTune task has none of those
            # -- one file, run directly by the scorer -- so the honest answer is "no stages", which
            # the prompt does not offer. Measured 2026-08-21 on a `google/gemini-3.7-flash` run: all
            # FIVE nodes emitted a manifest whose entire content was one invented stage,
            # `python -c "print('Ready')"` with `expect.assert: "Check solver environment
            # readiness"`, and `solver.py` was never written. The `stages` phase had also been the
            # single largest consumer of a DeepSeek run's wall clock (232 of its generations).
            #
            # `score` is the right name: it is RESERVED against a Developer manifest precisely
            # because it denotes the operator's own scoring step, and this is the operator's.
            "stages": [{
                "name": "score",
                "command": [
                    interpreter, str(BRIDGE),
                    "--algotune-root", str(root),
                    "--task", args.task,
                    "--model", "LoopLab",
                    "--solver", "solver.py",
                    # TRAIN, mirroring AlgoTuner's own agent. Scoring every node on the test split
                    # would let this arm optimise against the set it is graded on while the reference
                    # arm does not -- the champion is scored on test once, after the run.
                    "--subset", "train",
                ] + (["--enforce-rules"] if args.enforce_rules else []),
                # OPEN[bridge-deadline-equals-stage-deadline] the stage timeout and the bridge's
                # internal evaluator deadline are the same number, so the engine's SIGKILL always
                # fires first and the bridge's `evaluator_timeout` scored-zero verdict is
                # unreachable through the operator pipeline.
                # proof:absent:bridge_timeout@benchmarks/algotune/make_task.py
                # REVIEW 2026-08-30 (correctness): the command above passes no `--timeout`, so the
                # bridge runs its default 7200 s evaluator clock — started AFTER copy plus up to
                # 1800 s of build_ext — inside a stage whose own ceiling is the same `args.timeout`
                # (default 7200). Whenever the evaluator would hit the bridge's deadline, the stage
                # kill lands first: SIGKILL, no stdout, engine reason `timeout`, which is in
                # `metric_salvage.NEVER_SALVAGED_REASONS` — exactly the no-metric discard the
                # bridge's timeout branch was written to abolish ("a timed-out solver is a wrong
                # solver, not a missing measurement"). Same shape for the pinned dev command's
                # outer 450 s vs the inner default. Hand the bridge a deadline sized under the
                # stage's (stage ceiling minus copy/build margin) so the verdict path can ever run.
                "timeout": args.timeout,
            }],
            "metric": {"kind": "stdout_json", "key": "speedup"},
            # THE GRADER FENCE. AlgoTune is `uv pip install -e .` into the same venv the Developer
            # inspects, so without this `read_installed`/`grep_installed` reach the checker, the
            # timer and the scorer as easily as they reach numpy -- and measured 2026-08-20, one
            # node made 213 of its 216 env-inspection calls against exactly these two packages
            # (`is_solution`, `def run_isolated_benchmark`, `mean_speedup`,
            # `AlgoTuner.utils.isolated_benchmark`). A solver written after reading the checker is
            # not a result. `AlgoTuneTasks` is fenced beside `AlgoTuner` because it is where the
            # task classes and their instance-size parameters live, which is the specific thing the
            # Developer was hunting for.
            "protect_packages": ["AlgoTuner", "AlgoTuneTasks"],
            "timeout": args.timeout,
        },
    }

    spec_path = args.out_dir.resolve() / f"algotune_{args.task}.json"
    spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    print(spec_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
