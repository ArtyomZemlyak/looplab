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
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent / "looplab_eval.py"

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


# The clause that REPLACES `_DELIVER_NO_MEASURE`. It states what is now true, and states the cost,
# because an expensive capability offered without its price is used until the budget is gone.
MEASURE = (
    " YOU CAN MEASURE YOUR OWN SCORE, AND YOU SHOULD -- ON THE TRAIN SPLIT, THE SAME ONE EVERY NODE "
    "IS SCORED ON. `run_dev_command(\"eval_train\")` runs the REAL evaluator over the real "
    "instances with your currently staged files and prints the same JSON the scorer prints: "
    "`speedup`, `eval_seconds`, and whether every instance was valid. That is the number, not an "
    "estimate of it. Use it the way you would use a test: write the simplest correct solver, "
    "measure, change ONE thing, measure again. "
    "IT IS EXPENSIVE, AND IT IS CHARGED TO A CLOCK YOU CANNOT SEE. A real evaluation of this task "
    "takes roughly half a minute to six minutes on the same machine your solver is timed on, and "
    "your whole session is bounded at twenty minutes of wall clock -- so ONE call can be a third "
    "of everything you have, and a call that hangs takes that whether or not it answers. Budget it "
    "like that: write the solver FIRST, measure ONCE when you have something worth measuring, and "
    "never twice on the same code. A session that spends its clock measuring and ends with no file "
    "written has produced nothing at all. "
    "A guess you can check in one command is not a guess to write into the summary. "
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
    calls = (" No call to " + ", ".join(f"`{c}`" for c in disallowed) +
             " — the arena refuses runtime code generation and harness introspection outright, so "
             "an idea that turns on generating and compiling specialised code is not one it can "
             "accept, however fast the result would be." if disallowed else "")
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
                 + ((_DELIVER_WRITE if args.full_context else DELIVER) if args.deliver else "")
                 + (MEASURE if args.full_context else "")
                 + (ONE_CARD.format(task=args.task) if args.one_card else "")
                 # BANS then PERMISSIONS, in that order and both under the same flag: they are one
                 # statement of what this arena allows, and the half that was missing is the half
                 # that costs score. See `solution_space_clause`.
                 + (rules_clause(root) if args.enforce_rules else "")
                 + (solution_space_clause(ref, args.task) if args.enforce_rules else "")),
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
            "timeout": 450.0,
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
