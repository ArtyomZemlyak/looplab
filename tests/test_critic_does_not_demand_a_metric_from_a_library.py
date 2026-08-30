"""The critic must not demand an in-code metric from a candidate nothing runs.

MEASURED over the probe corpus on 2026-08-29: the `critic` detector ran on 34 nodes and raised
`critic:no_metric_output` on 34 of 34 — a 100 % false-positive rate — because an AlgoTune eval
stage runs `benchmarks/algotune/looplab_eval.py --solver solver.py` and the candidate is a LIBRARY
that prints nothing. dsIF6 alone carried the alarm on all six of its nodes, including the champion
that went on to score 205.8223 on the test split.

The obvious fix is the wrong one, and that was measured too: switching the check to the task's
DECLARED metric key (`eval.metric.key`) would take the false-positive rate from 208/213 to 213/213,
because AlgoTune's key is `speedup` and 0 of 213 solvers reference it while 5 mention `metric`.
What actually distinguishes the two worlds is WHO IS RUN: `entrypoint_candidates` resolves
`["python", "score.py"]` to `score.py` and the AlgoTune stage command to `[]`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from looplab.core.models import Idea  # noqa: E402
from looplab.trust.critic import critique, scorer_is_in_tree  # noqa: E402

# A real AlgoTune champion in miniature: a library, no printing, no `metric` anywhere.
SOLVER = """
import numpy as np

class Solver:
    def solve(self, problem, **kwargs):
        return {"factors": sorted(np.array(problem["composite"]).tolist())}
"""

ALGOTUNE_STAGE = ["/var/tmp/looplab-bench/AlgoTune/.venv/bin/python",
                  "/var/tmp/looplab-bench/looplab/benchmarks/algotune/looplab_eval.py",
                  "--task", "integer_factorization", "--solver", "solver.py", "--subset", "train"]


class _Eval:
    def __init__(self, command=None, stages=None):
        self.command = command or []
        self.stages = stages or []


class _Task:
    def __init__(self, ev):
        self.eval = ev


def _issues(code, **kw):
    return [r["issue"] for r in critique(Idea(operator="draft", params={}), code, **kw)]


def test_a_library_graded_by_a_harness_is_not_accused_of_hiding_the_metric():
    assert "no_metric_output" not in _issues(SOLVER, scorer_in_tree=False)


def test_the_same_library_is_still_flagged_when_the_scorer_is_its_own_file():
    assert "no_metric_output" in _issues(SOLVER, scorer_in_tree=True)


def test_the_hard_gate_survives_the_suppression():
    """`hardcoded_metric` excludes a node from selection; it must not ride along."""
    cheat = 'import json\nprint(json.dumps({"metric": 0.99}))\n'
    assert "hardcoded_metric" in _issues(cheat, scorer_in_tree=False)


def test_an_algotune_stage_command_resolves_to_no_in_tree_scorer():
    assert scorer_is_in_tree(_Task(_Eval(stages=[{"command": ALGOTUNE_STAGE}]))) is False


def test_a_classic_repo_task_still_reads_as_self_scoring():
    assert scorer_is_in_tree(_Task(_Eval(command=["python", "score.py"]))) is True
    assert scorer_is_in_tree(_Task(_Eval(command=["python", "-m", "pkg.score"]))) is True


def test_a_task_that_cannot_be_asked_keeps_todays_answer():
    assert scorer_is_in_tree(None) is True
    assert scorer_is_in_tree(_Task(None)) is True
