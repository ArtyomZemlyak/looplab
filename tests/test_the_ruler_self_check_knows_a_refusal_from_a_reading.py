"""Point 5 carries four numbers the sweep never checked, and point 2 says how a check can lie.

`ruler_check.py` verifies the SHAPE of the baseline cache — one regime, a full set of per-instance
timings. It says nothing about the READING: submit the reference itself and `speedup` must come back
~1.0, because both sides of `baseline_ms / optimized_ms` are then the same code. `baseline_ms` comes
from the cache (written once) and `optimized_ms` is timed now, so the self-speedup is the ratio of
"how fast this box was then" to "how fast it is today".

Measured 2026-09-04: `edge_expansion` 0.8861 against the sweep's 0.9847 (−10.0 %), four repeats
inside 0.875–0.899, and three more with the other lanes idle at 0.8865 — not load, drift.

The two ways this reading is not a reading are both in point 2's own words: a zero that arrives in a
second is the harness declining, not a slow solver. Both were hit while building the tool
(`solver_unloadable`, then `Task data directory not found`), and both had `eval_seconds` ~1.7
against a real ~28 s.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import ruler_selfcheck  # noqa: E402


def test_a_zero_that_arrives_in_a_second_is_a_refusal():
    assert ruler_selfcheck.refused({"speedup": 0.0, "eval_seconds": 1.7})
    assert ruler_selfcheck.refused({"speedup": 0.0, "eval_seconds": 0.1})


def test_a_named_refusal_is_named_even_when_it_looks_like_a_score():
    """`solver_unloadable` came back with a plausible-looking envelope; the reason field is the
    claim and it outranks the number."""
    why = ruler_selfcheck.refused(
        {"speedup": 0.0, "eval_seconds": 1.7,
         "no_speedup": {"reason": "solver_unloadable",
                        "evaluator_verdict": "No module named 'reference_edge_expansion'"}})
    assert "solver_unloadable" in why


def test_a_real_zero_from_a_real_evaluation_is_not_called_a_refusal():
    """A solver that genuinely fails validity scores 0.0 after a FULL evaluation — arm A's
    `pagerank` did, at 0.0 with 66 verification failures (§181). Calling that a harness refusal
    would erase a real result."""
    assert ruler_selfcheck.refused({"speedup": 0.0, "eval_seconds": 41.2}) == ""


def test_an_ordinary_reading_is_not_a_refusal():
    assert ruler_selfcheck.refused({"speedup": 0.8861, "eval_seconds": 27.6}) == ""


def test_the_solver_it_builds_is_self_contained(tmp_path):
    """`--solver-file-only` copies ONE file. The first version imported the reference module and
    every evaluation came back `solver_unloadable` in 1.7 s."""
    ref = tmp_path / "probes" / "p1" / "ws" / "edge_expansion"
    ref.mkdir(parents=True)
    (ref / "reference_edge_expansion.py").write_text(
        "class Task:\n    pass\n\n\n"
        "class EdgeExpansionTask(Task):\n"
        "    def solve(self, problem):\n        return {'edge_expansion': 1.0}\n",
        encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    path = ruler_selfcheck.build_solver("edge_expansion", str(out), str(tmp_path / "probes"))
    body = Path(path).read_text(encoding="utf-8")
    assert "import reference_edge_expansion" not in body and "from reference_" not in body, (
        "the reference is imported, not inlined; --solver-file-only leaves it behind")
    assert "class EdgeExpansionTask(Task)" in body, "the reference implementation is not in the file"
    ns: dict = {}
    exec(compile(body, path, "exec"), ns)                      # noqa: S102 - that IS the check
    assert ns["Solver"]().solve({"x": 1}) == {"edge_expansion": 1.0}, (
        "the built Solver does not delegate to the reference's own solve()")


def test_a_task_with_no_delivered_reference_is_an_error_not_a_number(tmp_path):
    try:
        ruler_selfcheck.build_solver("no_such_task", str(tmp_path), str(tmp_path))
    except FileNotFoundError:
        return
    raise AssertionError("a missing reference must refuse, not build an empty solver")
