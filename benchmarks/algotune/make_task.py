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
    "None of those names a change to solver.py, and this run gets three or four experiments in "
    "total. Those two files are also the WHOLE of the reading: the evaluator, the timer and the "
    "instance generator are fenced and are not yours to look at, and a solver written from them "
    "would not be a result.\n"
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


def rules_clause(root: Path) -> str:
    """The arena's submission rules, in the goal, DERIVED from the validator that enforces them.

    Not hand-written prose: a copied ban list goes stale in the direction that silently permits, and
    the operator then reads a goal that promises a rule the arena no longer has (or, worse, does not
    promise one it does). This reads `AlgoTuner.security.code_validator`'s own tables, so the
    sentence and the check cannot disagree.

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
            "can accept — pick a different one rather than a way around.")


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
                 + (ONE_CARD.format(task=args.task) if args.one_card else "")
                 # BANS then PERMISSIONS, in that order and both under the same flag: they are one
                 # statement of what this arena allows, and the half that was missing is the half
                 # that costs score. See `solution_space_clause`.
                 + (rules_clause(root) if args.enforce_rules else "")
                 + (solution_space_clause(ref, args.task) if args.enforce_rules else "")),
        "direction": "max",
        "editable_path": str(ws),
        "edit_surface": ["solver.py"],
        "protect": [ref_name, "description.txt"],
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
