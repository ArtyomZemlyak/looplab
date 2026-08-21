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
import json
import shutil
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
ROLE_SPLIT = (
    " ONE EXPERIMENT = ONE ALGORITHM. The Researcher names exactly one concrete algorithm per "
    "experiment and commits to it -- not a decision tree, not \"A or B depending on C\". If whether "
    "that algorithm is viable depends on something nobody here knows yet (the problem sizes, how "
    "much memory a table would take), the Researcher STATES THE ASSUMPTION as part of the idea and "
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
DELIVER = (
    " YOU CANNOT MEASURE YOUR OWN SCORE, AND YOU ARE NOT MEANT TO. The instances you are graded on "
    "are not on this machine and you cannot generate them; the speedup comes from the evaluator, "
    "which runs after you finish. So timing your own guesses against invented inputs measures "
    "something else -- your guess about the input -- and no amount of it brings the real number "
    "closer. (Correctness is different and you CAN check it: build a few instances yourself and "
    "verify the contract holds. Do that briefly, then stop.) "
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
# Three rules, and each names the behaviour it forbids rather than describing a virtue:
#   1. ONE CARD. Think about as many directions as you like; commit to exactly one.
#   2. TESTING IS THE EVALUATION. Not the probe, not a benchmark the agent writes for itself.
#   3. THE DEVELOPER IMPLEMENTS, and that is all it does.
ONE_CARD = (
    " HOW THIS RUN IS ORGANISED, and it is not negotiable:\n"
    "(1) ONE HYPOTHESIS. The Researcher may consider any number of directions in its own reasoning, "
    "but it ends its turn having committed to EXACTLY ONE concrete idea, and the run works that one "
    "idea. Not a menu, not \"A, and if that fails B\", not a decision tree with an unresolved branch "
    "condition. If the idea rests on something nobody here knows yet, STATE THE ASSUMPTION inside the "
    "idea and commit anyway -- an assumption that turns out wrong is a finished experiment with a "
    "result, which is what this loop reads and builds on.\n"
    "(2) THE ONLY TEST IS THE SUBMITTED CODE. Nothing is 'checked', 'verified', 'compared' or "
    "'benchmarked' by any other means. There is no exploratory probing to find out which approach is "
    "faster, no timing harness written on the side, no trying two variants to see which wins: you "
    "write the solver, you submit it, and the evaluation tells you. That report is the evidence, and "
    "it is the ONLY evidence. If you catch yourself measuring something in order to decide what to "
    "write, stop -- write the committed idea instead and let the evaluation answer.\n"
    "(3) THE DEVELOPER IMPLEMENTS THE IDEA IT WAS GIVEN. Mechanically, once, exactly as named. It "
    "does not pick between algorithms, does not benchmark alternatives, does not redesign the "
    "approach, and does not use any tool to decide WHAT to write -- only, at most, to make the named "
    "thing actually RUN (does this import, what shape does this API return). If the Developer "
    "believes the idea is wrong, it implements it anyway and says so in its rationale; a different "
    "idea belongs to the NEXT experiment, not to this one.\n"
    "Getting a submitted, evaluated solver -- even a mediocre one -- is worth more than any amount of "
    "investigation that produces none."
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--task", required=True)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--python", default=None,
                    help="Interpreter that has AlgoTune installed "
                         "(default: <algotune-root>/.venv/bin/python).")
    ap.add_argument("--timeout", type=int, default=7200, help="Eval timeout in seconds.")
    ap.add_argument("--one-card", action="store_true",
                    help="Append the ONE HYPOTHESIS / THE ONLY TEST IS THE SUBMITTED CODE / THE "
                         "DEVELOPER IMPLEMENTS clause (see ONE_CARD). Composes with the others.")
    ap.add_argument("--deliver", action="store_true",
                    help="Append the YOU CANNOT MEASURE YOUR OWN SCORE clause (see DELIVER).")
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

    spec = {
        "kind": "repo",
        "id": f"algotune_{args.task}",
        "goal": (GOAL.format(task=args.task)
                 + (ROLE_SPLIT if args.role_split else "")
                 + (DELIVER if args.deliver else "")
                 + (ONE_CARD if args.one_card else "")),
        "direction": "max",
        "editable_path": str(ws),
        "edit_surface": ["solver.py"],
        "protect": [ref_name, "description.txt"],
        "eval": {
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
            ],
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
