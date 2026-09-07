"""A power tool that cannot be run before the money is not a power tool.

§115's arm was sized from a table computed on a different outcome, a different task and a smaller
corpus, and closed at p = 0.1341 having answered nothing (§180). `benchmarks/arm_power.py` is the
replacement: it resamples the corpus's own champions and runs the arm's actual test.

The first version always enumerated the null — six batches x 300 trials is 14 million relabellings,
and it ran ten minutes without printing a row before being killed by pid. Hence `EXACT_NULL_CAP`
and the sampled branch, and hence these tests: the exact and sampled branches must agree, and the
tool must refuse a corpus too small to resample.
"""
from __future__ import annotations

import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import arm_power  # noqa: E402


def _batches(diff, n):
    """`n` batches where the treatment pair sits `diff` above the control pair."""
    return [([100.0 + diff, 120.0 + diff], [100.0, 120.0]) for _ in range(n)]


def test_a_flat_arm_is_not_significant():
    """Identical arms: the observed statistic is the middle of its own null."""
    p = arm_power.stratified_p(_batches(0.0, 3))
    assert 0.3 < p < 0.9, p


def test_an_arm_that_wins_every_batch_is():
    p = arm_power.stratified_p(_batches(500.0, 4))
    assert p <= 1 / 6 ** 4 + 1e-9, p


def test_the_sampled_branch_agrees_with_the_exact_one():
    """Above the cap the null is sampled; it must not answer a different question."""
    # A MIDDLING p, on purpose. The first version used a batch set whose exact p is 1/1296, and a
    # mutation making the sampled branch return 0.0 passed it -- |0 - 0.0008| is inside any
    # tolerance. Comparing where the null actually lives is the only version of this test that can
    # fail.
    b = _batches(0.0, 4)                        # 6**4 = 1296, exact, and p lands near the middle
    exact = arm_power.stratified_p(b)
    sampled = arm_power.stratified_p(b, draws=20000, rnd=random.Random(3))
    # force the sampled path on the same data by lowering the cap
    old = arm_power.EXACT_NULL_CAP
    try:
        arm_power.EXACT_NULL_CAP = 1
        forced = arm_power.stratified_p(b, draws=20000, rnd=random.Random(3))
    finally:
        arm_power.EXACT_NULL_CAP = old
    assert 0.2 < exact < 0.9, exact
    assert abs(forced - exact) < 0.03, (exact, forced)
    assert sampled == exact, "the exact branch stopped being taken under the cap"


def test_power_rises_with_batches_and_with_effect():
    scores = [50.0, 120.0, 180.0, 210.0, 260.0] * 8
    small = arm_power.power(scores, 20.0, 3, 60, 0.05)
    large = arm_power.power(scores, 200.0, 3, 60, 0.05)
    assert large > small, (small, large)
    few = arm_power.power(scores, 80.0, 2, 60, 0.05)
    many = arm_power.power(scores, 80.0, 5, 60, 0.05)
    assert many >= few, (few, many)


def test_it_refuses_a_corpus_too_small_to_resample(tmp_path, capsys):
    assert arm_power.main(["--root", str(tmp_path)]) == 2
    assert "refusing to simulate" in capsys.readouterr().err
