"""`no_valid_speedups` names two different worlds, and both were leaving as "not measured".

Nothing ran at all — the arena's failure, and no evidence about a solver in either direction. Or
everything ran and every answer was wrong — the candidate's failure, and under the arena's own rule
(100 % validity or nothing) a real zero that belongs in the mean, exactly as `compare_arms`'
docstring says of `spectral_clustering` at 95/100 valid.

`remPde4` on this box is the second kind: its `final.json` carries `is_solution_errors_distinct:
100` and lines like "max abs err=0.131, max rel err=1.39e+06". It was excluded from the means as an
arena failure, which drops earned failures out and biases an arm upward.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH / "algotune"))

import arm_readout  # noqa: E402
import compare_arms  # noqa: E402

EARNED = {"subset": "test", "speedup": 0.0,
          "no_speedup": {"reason": "no_valid_speedups",
                         "is_solution_errors_distinct": 100, "is_solution_error_lines": 122,
                         "is_solution_errors": [{"message": "max abs err=0.131", "count": 2}]}}
ARENA = {"subset": "test", "speedup": 0.0, "no_speedup": {"reason": "no_valid_speedups"}}


def _final(tmp, record) -> Path:
    p = Path(tmp) / "final.json"
    p.write_text(json.dumps(record), encoding="utf-8")
    return p


def test_the_bridge_row_separates_the_two_worlds():
    with tempfile.TemporaryDirectory() as tmp:
        got, why = compare_arms._arm_b_final(_final(tmp, EARNED))
        assert got == 0.0, (got, why)
        assert "every instance failed is_solution" in why, why
    with tempfile.TemporaryDirectory() as tmp:
        got2, why2 = compare_arms._arm_b_final(_final(tmp, ARENA))
        assert got2 is None, (got2, why2)      # nothing ran: not evidence about a solver
        assert why2 == "no_valid_speedups", why2


def test_the_evidence_is_read_and_not_the_word():
    """A row whose reason says `no_valid_speedups` and whose block is empty is the arena's; the two
    fixtures differ ONLY in the evidence, so a check keyed on the reason word passes both or
    neither."""
    assert compare_arms.validation_failed(EARNED["no_speedup"]) is True
    assert compare_arms.validation_failed(ARENA["no_speedup"]) is False
    assert compare_arms.validation_failed(None) is False
    assert compare_arms.validation_failed({"is_solution_errors": []}) is False


def test_the_arm_readout_admits_an_earned_zero(tmp_path, monkeypatch):
    """The same rule where the arm's own arithmetic happens. Measured on this box: `remPde4` is the
    only non-positive final and `assigned_cap` answers None for it, so §190's readout does not move
    -- the rule is fixed before a probe in a design needs it, not after."""
    root = tmp_path / "probes"
    (root / "earned").mkdir(parents=True)
    (root / "arena").mkdir()
    (root / "earned" / "final.json").write_text(json.dumps(EARNED), encoding="utf-8")
    (root / "arena" / "final.json").write_text(json.dumps(ARENA), encoding="utf-8")
    monkeypatch.setattr(arm_readout, "ROOT", str(root))
    assert arm_readout.score("earned") == (0.0, None)
    got, why = arm_readout.score("arena")
    assert got is None and "speedup is 0.0" in why, (got, why)


BUILT_WRONG = {"subset": "test", "speedup": 0.0,
               "no_speedup": {"reason": "evaluator_error",
                              "evaluator_verdict": "Agent-compatible evaluation error: Failed in "
                                                   "nopython mode pipeline (step: nopython frontend)",
                              "is_solution_errors": [
                                  {"message": "discrete_log/LoopLab-3095249: Failed in nopython "
                                              "mode pipeline (step: nopython frontend)",
                                   "count": 1}]}}


def test_the_sentence_says_which_kind_of_earned_zero_it_is():
    """§338. `remDL13`'s zero came back `evaluator_error … nopython frontend` -- the candidate's own
    @njit would not compile. Both kinds are the candidate's fault and both are real zeros; calling a
    compile failure "every instance failed is_solution" sends the reader after validation failures
    that do not exist."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        got, why = compare_arms._arm_b_final(_final(tmp, BUILT_WRONG))
        assert got == 0.0, (got, why)
        assert "the candidate's own code would not build or import" in why, why
    with tempfile.TemporaryDirectory() as tmp:
        got2, why2 = compare_arms._arm_b_final(_final(tmp, EARNED))
        assert got2 == 0.0 and "every instance failed is_solution" in why2, why2
