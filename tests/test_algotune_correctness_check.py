"""A cheap per-instance correctness check, because the writing sessions were checking speed.

MEASURED. Over three independent kcenters probes on 2026-08-28, of the agent's own `run_probe`
self-checks: dsKcCtl 55 probes / 3 compared to the reference solve / 1 called is_solution / 23
timed something; dsFBKc 42 / 3 / 1 / 13; fxKcenters 55 / 3 / 4 / 28. It times because timing is
cheap and the only correctness command on the card is a ~40 s full `eval_train`. dsKcCtl node 1 is
the bill: it knowingly traded exactness for speed, self-checked 55 times, and the ENGINE was the
first thing to say `Solution is not optimal. Found value: 33.955, Optimal value: 33.408`. 0.0, work
discarded, at 2-4 nodes per run.

These cases pin what the check must be: it validates rather than scores, it says WHY an instance
was rejected, a candidate that raises is a failed instance and not a crashed check, and it finds
the reference task by duck typing so it does not drag the arena in.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from benchmarks.algotune.looplab_check import check, find_task

# A whole task in miniature: the answer is the MINIMUM of the list, and `is_solution` demands
# optimality exactly as kcenters does -- which is the property the probes never checked.
_REFERENCE = '''
import logging

class MiniTask:
    def generate_problem(self, n, random_seed=0):
        return [(random_seed * 7 + i * 13) % 100 for i in range(max(3, n))]

    def solve(self, problem):
        return min(problem)

    def is_solution(self, problem, solution):
        best = min(problem)
        if solution != best:
            logging.error(f"Solution is not optimal. Found value: {solution}, Optimal value: {best}")
            return False
        return True

class NotATask:          # lacks is_solution -- must never be picked
    def generate_problem(self, n, random_seed=0): return []
    def solve(self, problem): return None
'''

_GOOD = "class Solver:\n    def solve(self, problem):\n        return min(problem)\n"
_SUBOPTIMAL = "class Solver:\n    def solve(self, problem):\n        return sorted(problem)[1]\n"   # fast, wrong
_RAISES = "class Solver:\n    def solve(self, problem):\n        raise ValueError('boom')\n"
_NO_CLASS = "def solve(problem):\n    return min(problem)\n"


def _files(tmp_path, solver_src):
    ref = tmp_path / "reference_mini.py"
    ref.write_text(_REFERENCE, encoding="utf-8")
    sol = tmp_path / "solver.py"
    sol.write_text(solver_src, encoding="utf-8")
    return ref, sol


def test_a_correct_solver_passes_every_instance(tmp_path):
    ref, sol = _files(tmp_path, _GOOD)
    out = check(ref, sol, n=4, size=6, seed=1)
    assert out["ok"] and out["valid"] == 4 and out["invalid"] == 0
    assert "NOT the score" in out["note"], "it must never be mistaken for the ruler"


def test_the_suboptimal_solver_the_probes_shipped_is_caught_and_the_reason_is_shown(tmp_path):
    """This is dsKcCtl node 1 in miniature: valid-looking, fast, and not optimal."""
    ref, sol = _files(tmp_path, _SUBOPTIMAL)
    out = check(ref, sol, n=3, size=6, seed=1)
    assert not out["ok"] and out["invalid"] == 3
    assert all("Solution is not optimal" in r.get("reason", "") for r in out["rows"]), \
        "the agent must be told WHY, not just that something failed"
    assert "score is 0 unless every instance validates" in out["note"]


def test_a_candidate_that_raises_is_a_failed_instance_not_a_crashed_check(tmp_path):
    ref, sol = _files(tmp_path, _RAISES)
    out = check(ref, sol, n=2, size=6, seed=1)
    assert not out["ok"] and out["invalid"] == 2
    assert all("ValueError: boom" in r.get("raised", "") for r in out["rows"])


def test_a_solver_without_a_Solver_class_is_reported_not_raised(tmp_path):
    ref, sol = _files(tmp_path, _NO_CLASS)
    out = check(ref, sol, n=1, size=6, seed=1)
    assert out["ok"] is False and "no `Solver` class" in out["error"]


def test_the_reference_task_is_found_by_its_three_methods(tmp_path):
    ref, _ = _files(tmp_path, _GOOD)
    import importlib.util
    spec = importlib.util.spec_from_file_location("_ref_probe", ref)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert find_task(mod).__name__ == "MiniTask", "NotATask has no is_solution and must be skipped"


def test_the_check_leaves_root_logging_as_it_found_it(tmp_path):
    """It attaches a handler to capture `is_solution`'s rejection; it must not keep it."""
    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    ref, sol = _files(tmp_path, _SUBOPTIMAL)
    check(ref, sol, n=2, size=6, seed=1)
    assert list(root.handlers) == before_handlers and root.level == before_level


def test_it_runs_as_a_command_and_prints_json(tmp_path):
    ref, sol = _files(tmp_path, _SUBOPTIMAL)
    proc = subprocess.run([sys.executable, "benchmarks/algotune/looplab_check.py",
                           "--reference", str(ref), "--solver", str(sol), "--n", "2", "--size", "6"],
                          capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["invalid"] == 2 and not payload["ok"]
