"""The AlgoTune goal card's opt-in clauses, pinned on the GENERATED GOAL rather than on the source.

Two defects measured on a real 20-task run (`/var/tmp/looplab-bench/runs-armb`, the
`--deliver --one-card --enforce-rules` arm, 2026-08-21):

(a) THE FIRST CARD WENT ON PROCEDURE. `max_independent_set_cpsat`'s first hypothesis was
    "PREREQUISITE (must happen before any experiment): read reference_max_independent_set_cpsat.py
    to extract the exact contract"; `queens_with_obstacles`' was "FIRST ACTION (blocking): Have the
    Developer read reference_queens_with_obstacles.py and record the exact contract". On
    `convex_hull`, `kcenters` and `pagerank` that shape reached card-0 itself. Under `--one-card` a
    card IS the experiment and a $1.00 task buys three or four of them.

(b) THE REFERENCE'S OWN LIBRARY WAS NEVER CONSIDERED. On the four tasks whose reference is CP-SAT
    and nothing else, every hypothesis was a hand-written exact search in pure Python; best scores
    0.2933 / 0.3101 / 0.3592 / 0.3470. Where the family WAS proposed (`multi_dim_knapsack`,
    `rectanglepacking`) it scored 0.4950 / 0.3740 — the top of that cluster. The old rules clause
    closed with "Write ordinary algorithmic Python", which on such a task reads as a prohibition.

Pinned on the OUTPUT because that is the artifact: these clauses are prose the operator ships to a
model, the property is "the shipped goal says it", and a source pin would be one comment away from
vacuous (CLAUDE.md's tier-1 rule — drive the thing and read the effect). The generator is run as a
real subprocess against a HERMETIC fake AlgoTune root, so the suite stays offline and the derivation
(`rules_clause` reads the validator's table, `solution_space_clause` reads the reference's imports)
is exercised rather than mocked.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKE_TASK = ROOT / "benchmarks" / "algotune" / "make_task.py"

# Nine or more, because `rules_clause` renders `protected[:8]` plus "and N more" and a shorter table
# would make N negative — the fake has to be able to reproduce the real sentence.
_FAKE_VALIDATOR = '''
class TamperingDetector:
    PROTECTED_MODULES = {
        "__builtins__", "ast", "builtins", "collections", "ctypes_not_this_one",
        "numpy", "ortools", "os", "scipy", "sys", "sympy", "typing",
    }
    # The SECOND table. `rules_clause` was silent about it for the whole first campaign -- see
    # `test_the_ban_on_runtime_code_generation_is_stated`.
    DISALLOWED_CALLS = {"exec", "eval", "compile", "gc.get_objects"}
'''

# The same fake with the second table REMOVED, so the "no table, no claim" half is driven rather
# than argued: an arena that stops having the rule must stop being quoted as having it.
_FAKE_VALIDATOR_NO_CALLS = "\n".join(
    line for line in _FAKE_VALIDATOR.splitlines()
    if "DISALLOWED_CALLS" not in line and not line.strip().startswith("#")) + "\n"

# Imports in the four shapes that matter to `reference_libraries`: plain, dotted `from`, an aliased
# stdlib module, and the registration boilerplate every real reference carries.
_FAKE_REFERENCE = '''
import logging
import random as rng
import numpy as np

from ortools.sat.python import cp_model

from AlgoTuneTasks.base import register_task, Task


class Ref(Task):
    def solve(self, problem):
        return []

    def is_solution(self, problem, solution):
        return True
'''

_STDLIB_ONLY_REFERENCE = '''
import itertools
from AlgoTuneTasks.base import Task


class Ref(Task):
    def solve(self, problem):
        return []
'''


def _make_root(tmp_path: Path, task: str, reference: str = _FAKE_REFERENCE,
               validator: str = _FAKE_VALIDATOR) -> Path:
    root = tmp_path / "AlgoTune"
    sec = root / "AlgoTuner" / "security"
    sec.mkdir(parents=True, exist_ok=True)   # several tests build the same root more than once
    (root / "AlgoTuner" / "__init__.py").write_text("", encoding="utf-8")
    (sec / "__init__.py").write_text("", encoding="utf-8")
    (sec / "code_validator.py").write_text(validator, encoding="utf-8")
    task_dir = root / "AlgoTuneTasks" / task
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "description.txt").write_text("Find the thing.\n", encoding="utf-8")
    (task_dir / f"{task}.py").write_text(reference, encoding="utf-8")
    return root


def _goal(tmp_path: Path, *flags: str, task: str = "fake_task",
          reference: str = _FAKE_REFERENCE, validator: str = _FAKE_VALIDATOR) -> str:
    """Run the REAL generator and return the goal it wrote."""
    root = _make_root(tmp_path, task, reference, validator)
    # One workspace per flag combination: `make_task` never clobbers an existing solver.py, so
    # sharing a directory between two calls in one test would hide a stub regression.
    out = tmp_path / ("ws_" + ("_".join(f.lstrip("-") for f in flags) or "default"))
    subprocess.run([sys.executable, str(MAKE_TASK), "--algotune-root", str(root),
                    "--task", task, "--out-dir", str(out), *flags],
                   check=True, capture_output=True, text=True)
    return json.loads((out / f"algotune_{task}.json").read_text(encoding="utf-8"))["goal"]


def _module():
    spec = importlib.util.spec_from_file_location("_algotune_make_task", MAKE_TASK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- defect (a): the wasted card


def test_reading_the_reference_is_stated_as_a_precondition_not_an_experiment(tmp_path):
    """The clause has to say the two things the run got wrong: that reading is required BEFORE the
    idea, and that it does not cost a card. Either half alone reproduces the defect — "read it
    first" without "free" is exactly what the Researcher turned into hypothesis #1."""
    goal = _goal(tmp_path, "--one-card")
    assert "PRECONDITION, NOT AN EXPERIMENT" in goal
    assert "COSTS NO CARD" in goal
    assert "BEFORE you commit an idea" in goal
    assert "`reference_fake_task.py`" in goal, "the clause must name the task's real file"
    assert "`description.txt`" in goal


@pytest.mark.parametrize("spelling", [
    # Verbatim from the run: the shape recurs under new names, so the clause names the names.
    "first read the reference",
    "extract the exact contract",
    "establish a correctness baseline harness",
    "land a faithful port so the contract becomes readable",
])
def test_the_non_experiment_spellings_the_run_actually_produced_are_named(tmp_path, spelling):
    assert spelling in _goal(tmp_path, "--one-card")


def test_the_precondition_does_not_reopen_the_read_fence(tmp_path):
    """The counter-risk, and it is measured too: an earlier run made 119 probe calls, 116 of them
    reading the grader, which is why `protect_packages` and the read fence exist. An invitation to
    read must arrive bounded — two named files, and an explicit statement that the harness is not
    among them — or it licenses the spree it is standing next to."""
    goal = _goal(tmp_path, "--one-card")
    assert "WHOLE of the reading" in goal
    # The harness must still be named as out of bounds...
    assert "the evaluator and the timer are" in goal and "fenced" in goal

    # ...but the card may not claim a fence it does not have. `make_task.py` copies the task module
    # into the workspace VERBATIM, and on the real tasks the instance generator is about two thirds
    # of it (11,681 of 17,677 characters on `convex_hull`). The card used to tell the model that the
    # generator "is fenced and not yours to look at" while handing it over in the same breath — a
    # statement the model can check and find false, in the one document it is supposed to trust.
    # So this is a two-way pin: whatever the card says about the generator must match what the
    # generator actually does, and it is derived from the copy rather than restated.
    import re as _re
    ws_ref = next(p for p in tmp_path.rglob("reference_*.py")
                  if "ws_" in str(p.parent.parent) or "ws_" in str(p.parent))
    shipped = ws_ref.read_text(encoding="utf-8")
    generator_is_shipped = "def generate_problem" in shipped
    claims_fenced = _re.search(r"instance generator[^.]{0,80}fenced", goal) is not None
    assert not (generator_is_shipped and claims_fenced), (
        "the card calls the instance generator fenced while shipping it in the workspace")
    if generator_is_shipped:
        assert "DOES contain this task's instance generator" in goal, (
            "the generator is in the workspace, so the card must say so rather than imply otherwise")
    # And it must not have grown into a general licence to investigate.
    for licence in ("investigate the environment", "read as much as you", "explore the workspace"):
        assert licence not in goal.lower()


# ---------------------------------------------------------------- defect (b): the excluded family


def test_the_rules_clause_no_longer_tells_the_solver_to_avoid_libraries(tmp_path):
    """NEGATIVE pin, substring on purpose (CLAUDE.md): what must not come back is the TEXT. On a
    task whose reference IS a solver-library call, "Write ordinary algorithmic Python" is read as
    "do not call it", and four tasks' worth of hypotheses obeyed."""
    assert "Write ordinary algorithmic Python" not in _goal(tmp_path, "--enforce-rules")
    assert "Write ordinary algorithmic Python" not in MAKE_TASK.read_text(encoding="utf-8")


def test_the_ban_on_protected_modules_is_stated_as_patching_not_as_using(tmp_path):
    goal = _goal(tmp_path, "--enforce-rules")
    assert "may not REASSIGN those modules' attributes" in goal
    assert "importing them and calling them normally" in goal


def test_the_solution_space_names_this_task_s_own_libraries(tmp_path):
    """DERIVED per task, not hand-written: the same clause must say `ortools` here and `scipy`
    there, or it is a claim somebody has to maintain by hand and will not."""
    goal = _goal(tmp_path, "--enforce-rules")
    assert "WHAT THE SOLUTION SPACE INCLUDES" in goal
    assert "`ortools`" in goal and "`numpy`" in goal
    assert "not a loophole" in goal
    # The permitted moves, named as a family and not as an answer.
    for lever in ("re-modelling", "changing how it is configured", "warm start"):
        assert lever in goal
    # ... and the one import that is still forbidden stays forbidden.
    assert "the library, never the reference" in goal


def test_a_pure_stdlib_reference_makes_no_claim_about_libraries(tmp_path):
    """No borrowed library, no sentence. The clause is a fact about THIS reference; inventing one
    for a task that imports nothing would be the hand-written prose this derivation replaces."""
    goal = _goal(tmp_path, "--enforce-rules", reference=_STDLIB_ONLY_REFERENCE)
    assert "ARENA RULES FOR THE SUBMITTED SOLVER" in goal, "the ban half still applies"
    assert "WHAT THE SOLUTION SPACE INCLUDES" not in goal


@pytest.mark.parametrize("source,expected", [
    ("import numpy\nimport os\n", ["numpy"]),
    ("from ortools.sat.python import cp_model\n", ["ortools"]),
    ("import scipy.linalg as sla\nimport numpy as np\n", ["numpy", "scipy"]),
    # Never the harness: `from AlgoTuneTasks.base import Task` is the one import a candidate may not
    # copy, so naming it as "available" would contradict the fence two clauses up.
    ("from AlgoTuneTasks.base import Task\nimport AlgoTuner\n", []),
    ("from __future__ import annotations\nimport itertools\n", []),
    ("from . import sibling\n", []),
    ("def f(:\n", []),                       # unparseable reference -> no claim at all
])
def test_the_library_list_is_the_reference_s_own_imports(tmp_path, source, expected):
    ref = tmp_path / "reference_x.py"
    ref.write_text(source, encoding="utf-8")
    assert _module().reference_libraries(ref) == expected


# ---------------------------------------------------------------- both clauses stay OPT-IN


def test_the_default_goal_is_unchanged_by_either_clause(tmp_path):
    """The whole point of the flags: a LoopLab task that is not an AlgoTune arm inherits nothing.
    Compared against `GOAL` itself, so this stays true if the clauses are later reordered."""
    goal = _goal(tmp_path)
    assert goal == _module().GOAL.format(task="fake_task")


@pytest.mark.parametrize("flag,marker", [
    ("--one-card", "PRECONDITION, NOT AN EXPERIMENT"),
    ("--enforce-rules", "WHAT THE SOLUTION SPACE INCLUDES"),
])
def test_each_new_clause_appears_only_under_its_own_existing_flag(tmp_path, flag, marker):
    assert marker in _goal(tmp_path, flag)
    for other in ("--deliver", "--role-split"):
        assert marker not in _goal(tmp_path, other), f"{marker} leaked into {other}"
    assert marker not in _goal(tmp_path)


@pytest.mark.parametrize("flags", [
    (), ("--role-split",), ("--deliver",), ("--one-card",), ("--enforce-rules",),
    ("--deliver", "--one-card", "--enforce-rules"),
])
def test_no_clause_ships_an_unsubstituted_placeholder(tmp_path, flags):
    """`ONE_CARD` acquired a `{task}` slot and is now `.format()`ed like `GOAL`. A future clause
    written with a literal brace would ship `{task}` to the model (or raise KeyError on a stray
    `{`), and neither is visible in a source read."""
    goal = _goal(tmp_path, *flags)
    assert "{" not in goal and "}" not in goal


# ------------------------------------------------------------------------------------------------
# defect (c): the rules clause promised a complete list and read one table of three
# ------------------------------------------------------------------------------------------------
# `rules_clause`'s docstring said "This reads `AlgoTuner.security.code_validator`'s own tables, so
# the sentence and the check cannot disagree." It read `PROTECTED_MODULES` and nothing else, so
# `DISALLOWED_CALLS` -- `exec`, `eval`, `compile`, `gc.get_objects` -- reached no goal card ever
# shipped. Checked against the live campaign's own `ws-B/algotune_spectral_clustering.json`: the
# letters exec/eval/compile occur ten times in that goal and every one of them is "evaluator".
#
# The cost is one experiment. `--enforce-rules` makes the bridge print
# `looplab_failure_reason: rules_violation`, which `engine/triage.py::DECLARABLE_REASONS` ends the
# node on WITHOUT spending a repair -- and a $1.00 run buys three or four cards.


def test_the_ban_on_runtime_code_generation_is_stated(tmp_path):
    goal = _goal(tmp_path, "--enforce-rules")
    for call in ("`exec`", "`eval`", "`compile`", "`gc.get_objects`"):
        assert call in goal, f"{call} is refused by the arena and the goal does not say so"


def test_an_arena_without_that_table_is_not_quoted_as_having_the_rule(tmp_path):
    """DERIVED means derived in both directions: no table, no sentence. A hand-written list would
    keep promising a rule the arena dropped, which is the half of staleness that costs nothing to
    the solver and everything to the operator reading the goal."""
    goal = _goal(tmp_path, "--enforce-rules", validator=_FAKE_VALIDATOR_NO_CALLS)
    assert "ARENA RULES" in goal, "the rest of the clause must survive"
    assert "`exec`" not in goal and "runtime code generation" not in goal, goal


def test_the_calls_ban_rides_with_enforce_rules_and_not_otherwise(tmp_path):
    assert "`gc.get_objects`" not in _goal(tmp_path)
    assert "`gc.get_objects`" not in _goal(tmp_path, "--one-card")


# The REAL arena, when one is on the box: every prohibition the goal states is one the shipped
# validator actually enforces, checked by RUNNING it on code that breaks the rule. This is the only
# form of this check that cannot drift -- a table read and a sentence written can still disagree
# about what the AST visitor does, and three of the four prohibitions have no table at all.
_REAL_ROOT = Path("/var/tmp/looplab-bench/AlgoTune")
_PROBES = {
    "`import ctypes`": "import ctypes\nclass Solver:\n    def solve(self, p): return 1\n",
    "`sys.modules`": ("import sys\nclass Solver:\n"
                      "    def solve(self, p): return sys.modules['os']\n"),
    "`is_solution`": ("class Solver:\n    def solve(self, p):\n"
                      "        p.is_solution = lambda *a: True\n        return 1\n"),
    "`compile`": ("class Solver:\n    def solve(self, p):\n"
                  "        return compile('1', '<s>', 'eval')\n"),
    "`eval`": "class Solver:\n    def solve(self, p): return eval('1')\n",
    "`exec`": "class Solver:\n    def solve(self, p):\n        exec('x=1')\n        return 1\n",
}


@pytest.mark.skipif(not (_REAL_ROOT / "AlgoTuner" / "security" / "code_validator.py").exists(),
                    reason="no AlgoTune checkout on this box")
@pytest.mark.parametrize("named,code", sorted(_PROBES.items()))
def test_every_prohibition_the_goal_states_is_one_the_real_validator_enforces(tmp_path, named, code):
    """Tier 1: the goal says the arena refuses this, so hand the arena code that does it.

    A goal that forbids what the arena permits excludes a family for nothing -- the measured cost of
    the retired "write ordinary algorithmic Python" sentence, four tasks stuck at 0.29-0.36.
    """
    goal = _goal(tmp_path, "--enforce-rules")
    assert named.strip("`").split(".")[0] in goal or named in goal, (
        f"{named} is no longer in the goal; delete its probe or restore the sentence")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1])\n"
         "from AlgoTuner.security.code_validator import check_code_for_tampering as chk\n"
         "print(repr(chk(sys.argv[2])))",
         str(_REAL_ROOT), code],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() != "None", (
        f"the goal tells the model {named} is NOT SCORED and this arena scores it:\n{code}")


@pytest.mark.skipif(not (_REAL_ROOT / "AlgoTuner" / "security" / "code_validator.py").exists(),
                    reason="no AlgoTune checkout on this box")
def test_a_plain_solver_is_not_refused_by_the_arena():
    """The other direction, so the probes above are not passing because everything is refused."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1])\n"
         "from AlgoTuner.security.code_validator import check_code_for_tampering as chk\n"
         "print(repr(chk(sys.argv[2])))",
         str(_REAL_ROOT),
         "import numpy as np\nclass Solver:\n"
         "    def solve(self, p): return np.asarray(p['a']).sum()\n"],
        capture_output=True, text=True, timeout=120)
    assert proc.stdout.strip() == "None", proc.stdout + proc.stderr


@pytest.mark.skipif(not (_REAL_ROOT / "AlgoTuner" / "security" / "code_validator.py").exists(),
                    reason="no AlgoTune checkout on this box")
def test_the_calls_the_goal_names_are_exactly_the_calls_the_real_arena_refuses(tmp_path):
    """BOTH directions against the shipped table, parsed back out of the shipped SENTENCE.

    Under-stating costs the solver an experiment (that is defect (c)); over-stating excludes a
    family for nothing (that is defect (b), measured at four tasks stuck between 0.29 and 0.36). A
    list typed into the prose can drift either way and this is the check that notices, because it
    reads the goal that was actually generated and the table that is actually enforced.
    """
    root = _make_root(tmp_path, "fake_task")
    real = json.loads(subprocess.run(
        [sys.executable, "-c",
         "import json, sys; sys.path.insert(0, sys.argv[1])\n"
         "from AlgoTuner.security.code_validator import TamperingDetector as T\n"
         "print(json.dumps(sorted(T.DISALLOWED_CALLS)))", str(_REAL_ROOT)],
        capture_output=True, text=True, timeout=120, check=True).stdout)
    module = _module()
    clause = module.rules_clause(_REAL_ROOT)
    match = re.search(r"No call to ([^—]+)—", clause)
    assert match is not None, f"the calls sentence is gone from the clause:\n{clause[-400:]}"
    named = sorted(re.findall(r"`([^`]+)`", match.group(1)))
    assert named == sorted(real), (
        f"the goal names {named} and the arena refuses {sorted(real)}; a goal that forbids what the "
        "arena permits excludes a family for nothing, and one that permits what it forbids costs a "
        "node")
    assert root  # the hermetic root is built so this test cannot pass by accident on an empty tree
