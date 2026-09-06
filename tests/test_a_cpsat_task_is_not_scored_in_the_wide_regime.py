"""A candidate that changes nothing scores about 1.5 on a CP-SAT task at twenty-two workers.

§314 measured it, twice, with the predictions recorded first: re-timing the baseline on an idle box
moved it 2 %, and reading that quiet baseline on that quiet box still gave 1.5291. At one evaluation
worker the same code reads 0.9922, and the spread collapses from +-23 % to +-2.5 %. So the excess is
an asymmetry between the baseline pass and the candidate pass that only exists at twenty-two, and it
grows with the task's own timing variance -- invisible on the ten deterministic tasks, 50 % here.

The knowledge was in the inventory and nothing stopped a campaign from printing the number anyway.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH / "algotune"))
sys.path.insert(0, str(BENCH))

import looplab_eval  # noqa: E402

CPSAT = "from ortools.sat.python import cp_model\nclass X:\n    def solve(self, p): return cp_model\n"
PLAIN = "import numpy as np\nclass X:\n    def solve(self, p): return np.zeros(3)\n"


def _key(monkeypatch, workers):
    """The key THE EVALUATOR would write, not a hand-typed one.

    The first version of this test typed `lane22r3`; `eval_regime()` returns `__lane22r3`, so the
    guard's `startswith("lane")` was False for the serial regime and it refused the very run its
    own message tells the operator to make. A hand-typed fixture agreed with the bug because both
    were written by the same wrong idea about the key's shape.
    """
    monkeypatch.setenv("ALGOTUNE_EVAL_WORKERS", workers)
    return looplab_eval.eval_regime()["key"]


def test_a_cpsat_reference_may_not_be_scored_twenty_two_wide(monkeypatch):
    wide = _key(monkeypatch, "auto")
    assert wide and "w" in wide.lstrip("_")[:1], wide
    assert looplab_eval.regime_scores_this_task(CPSAT, wide) is False


def test_the_same_reference_scores_at_one_worker(monkeypatch):
    serial = _key(monkeypatch, "1")
    assert serial and serial.lstrip("_").startswith("lane"), serial
    assert looplab_eval.regime_scores_this_task(CPSAT, serial) is True


def test_a_deterministic_reference_is_untouched(monkeypatch):
    """THE FIXTURE THAT DISAGREES WITH THE BUG. A guard that refused every wide run would pass the
    first test and break the ten tasks that rule as is -- `edge_expansion` reads 0.9950 at
    twenty-two workers and is scored there."""
    for workers in ("auto", "1"):
        assert looplab_eval.regime_scores_this_task(PLAIN, _key(monkeypatch, workers)) is True


def test_the_rule_reads_the_reference_and_not_a_list_of_task_names(monkeypatch):
    """A name list goes stale the first time upstream adds a solver; this box has already been bitten
    by a name-keyed check. `ortools` alone is enough -- a task importing it without `cp_model` is the
    same nondeterministic search."""
    assert looplab_eval.regime_scores_this_task("import ortools\n",
                                                _key(monkeypatch, "auto")) is False


def test_the_refusal_is_wired_into_the_evaluator_with_its_own_reason():
    src = (BENCH / "algotune" / "looplab_eval.py").read_text(encoding="utf-8")
    # The vocabulary is a closed list read by `compare_arms`; a reason missing from it is a score
    # that reads as an unexplained null downstream.
    assert '"regime_not_scorable_for_task",' in src
    assert "_regime_not_for_this_task()" in src
    assert "ALGOTUNE_SCORE_ANYWAY" in src
    arms = (BENCH / "algotune" / "compare_arms.py").read_text(encoding="utf-8")
    assert "regime_not_scorable_for_task" in arms
