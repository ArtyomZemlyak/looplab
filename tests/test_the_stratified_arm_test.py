"""The pre-registered statistic of §144, in a file with tests instead of in a shell one-liner.

§144 measured a between-batch component as large as the effect the arm hunts, and amended §142's
pooled Fisher to a batch-stratified exact test. §143 is the record of what hand-rolling a
pre-registered statistic per sweep costs. So it lives here, and these are the properties it must
have.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from benchmarks.stratified_arm import hypergeom_pmf, stratified_p  # noqa: E402


def test_the_hypergeometric_sums_to_one():
    for n1, n2, k in ((2, 2, 2), (3, 5, 4), (1, 1, 1), (4, 4, 0)):
        assert abs(sum(hypergeom_pmf(n1, n2, k).values()) - 1.0) < 1e-12


def test_one_stratum_reproduces_fisher():
    """With a single stratum the conditional test IS Fisher's exact test; a 2x2 of 2/2 against 0/2
    has one-sided p = 1/6."""
    p, obs, exp = stratified_p([(2, 2, 0, 2)])
    assert abs(p - 1 / 6) < 1e-12 and obs == 2 and abs(exp - 1.0) < 1e-12


def test_two_agreeing_strata_beat_one():
    """The whole point of stratifying: the same imbalance seen twice is stronger than seen once,
    even though pooling those two tables would say something different."""
    one, _, _ = stratified_p([(2, 2, 0, 2)])
    two, _, _ = stratified_p([(2, 2, 0, 2), (2, 2, 0, 2)])
    assert two < one
    assert abs(two - (1 / 6) ** 2) < 1e-12


def test_a_stratum_where_both_arms_agree_cannot_move_the_p():
    """A batch in which both arms did the same thing carries no information about the difference.
    MUTATION GUARD: an implementation that let such a batch contribute would make a p shrink by
    adding uninformative probes."""
    base, _, _ = stratified_p([(2, 2, 0, 2)])
    with_tie, _, _ = stratified_p([(2, 2, 0, 2), (2, 2, 2, 2)])
    assert abs(with_tie - base) < 1e-12
    with_zero, _, _ = stratified_p([(2, 2, 0, 2), (0, 2, 0, 2)])
    assert abs(with_zero - base) < 1e-12


def test_pooling_and_stratifying_can_disagree():
    """Simpson's shape, which is the reason §144 amended the statistic rather than the sample size:
    every stratum favours A while the pooled table does not."""
    strata = [(1, 1, 3, 5), (4, 5, 0, 1)]
    strat, _, _ = stratified_p(strata)
    pooled, _, _ = stratified_p([(5, 6, 3, 6)])
    assert strat < pooled


def test_the_expected_count_is_reported_and_sane():
    _, obs, exp = stratified_p([(2, 2, 0, 2), (1, 2, 1, 2)])
    assert obs == 3
    assert 1.0 < exp < 3.0
