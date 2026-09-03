"""A feature that IS the target under a monotone transform is a leak.

`target_leakage` was one Pearson coefficient. A column that is the target cubed, logged, exponentiated
or quantile-bucketed measured well below the threshold and the gate reported CLEAN — and this gate can
ABORT a run (`engine/audit.py::Engine._leakage_verdicts` returns True on it), so the operator reads
silence as assurance. That is worse than having no detector at all, and this family is the
differentiator: no library models ML-pipeline leakage, which is why these are custom.

The rank rung is not a NEW judgment. A column ranking identically to the target is the answer written
in different units, exactly as a column correlating 1.0 with it is, so it shares the threshold rather
than getting one nobody has calibrated.
"""
from __future__ import annotations

import math

import pytest

from looplab.trust.leakage import _finite_pairs, _pearson, _ranks, _spearman, target_leakage

_Y = [float(i) for i in range(1, 21)]


def _flag(col, target=None):
    return target_leakage({"f": col}, target if target is not None else _Y)


def test_a_CUBED_target_is_a_leak():
    """THE DEFECT, at the magnitude that made it dangerous: Pearson measures 0.922 — comfortably
    under the 0.98 bar — on a column that is literally the target cubed.

    MUTATION: drop the rank rung -> `leak: False`, and the run proceeds training on its own label.
    """
    verdict = _flag([v ** 3 for v in _Y])
    assert verdict["leak"] is True
    assert verdict["flagged_detail"]["f"]["rung"] == "monotone"
    assert verdict["flagged_detail"]["f"]["pearson"] < 0.98
    assert verdict["flagged_detail"]["f"]["spearman"] == pytest.approx(1.0)


@pytest.mark.parametrize("name, fn", [
    ("log", math.log),
    ("exp", lambda v: math.exp(v / 4.0)),
    ("sqrt", math.sqrt),
    ("reciprocal-negated", lambda v: -1.0 / v),
])
def test_every_monotone_reparameterization_is_caught(name, fn):
    """The class, not one member. Each of these is the target in different units."""
    assert _flag([fn(v) for v in _Y])["leak"] is True, name


def test_a_LINEAR_leak_is_still_caught_and_still_says_so():
    """The rung that already worked. MUTATION: replace Pearson with Spearman -> a genuinely linear
    leak still flags, so the swap would look harmless, while the reported coefficient stops being
    the one the threshold was calibrated on."""
    verdict = _flag([2.0 * v + 1.0 for v in _Y])
    assert verdict["leak"] is True
    assert verdict["flagged_detail"]["f"]["rung"] == "linear"
    assert verdict["flagged_detail"]["f"]["pearson"] == pytest.approx(1.0)


def test_an_HONEST_feature_is_still_clean():
    """Strictness that refuses a correct dataset is not strictness — and here it is worse than
    that, because a false leak ABORTS the run before any node evaluates."""
    noise = [float((i * 7919) % 23) for i in range(len(_Y))]
    assert _flag(noise)["leak"] is False
    assert _flag([1.0] * len(_Y))["leak"] is False, "a constant column correlates with nothing"


def test_a_WEAKLY_monotone_feature_is_clean():
    """The bar is 0.98 on both rungs. A feature that merely trends with the target — which is what
    a useful feature looks like — must not read as one."""
    trending = [v + (5.0 if i % 3 == 0 else -5.0) for i, v in enumerate(_Y)]
    verdict = _flag(trending)
    assert verdict["leak"] is False, verdict


def test_the_flagged_shape_is_unchanged_for_existing_readers():
    """`flagged` stays `{name: coefficient}`; `flagged_detail` is ADDITIVE. A reader that renders
    the old field must not start seeing a dict where it expects a float."""
    verdict = _flag([2.0 * v for v in _Y])
    assert isinstance(verdict["flagged"]["f"], float)
    assert set(verdict) == {"detector", "leak", "threshold", "flagged", "flagged_detail"}


def test_both_coefficients_describe_the_SAME_rows():
    """A leak flagged by one rung and cleared by the other, on different row sets, is not a
    comparison anybody can act on — so both run over `_finite_pairs`' cleaned pairs.

    MUTATION: rank the raw columns -> a NaN or a ragged column ranks rows the other coefficient
    never saw.
    """
    ragged = [1.0, 2.0, float("nan"), 4.0, 5.0, 6.0, 7.0]
    target = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert len(_finite_pairs(ragged, target)) == 4
    assert _spearman(ragged, target) == pytest.approx(1.0)
    assert _pearson(ragged, target) == pytest.approx(1.0)


def test_a_NaN_cannot_hide_a_monotone_leak():
    """The sibling property `_pearson` already holds, extended: a false negative is the dangerous
    direction for a gate that reports assurance."""
    poisoned = [v ** 3 for v in _Y]
    poisoned[0] = float("nan")
    assert _flag(poisoned)["leak"] is True


def test_ties_share_a_rank():
    """MUTATION: give ties arbitrary distinct ranks -> a constant column looks perfectly ordered
    against anything, and every dataset with one aborts."""
    assert _ranks([5.0, 5.0, 5.0]) == [2.0, 2.0, 2.0]
    assert _ranks([3.0, 1.0, 2.0]) == [3.0, 1.0, 2.0]
    assert _ranks([1.0, 1.0, 4.0, 4.0]) == [1.5, 1.5, 3.5, 3.5]


def test_too_few_rows_abstains():
    """Two points are always perfectly rank-ordered, which is meaningless against a threshold —
    the same floor `_pearson` already applies."""
    assert _spearman([1.0, 2.0], [1.0, 2.0]) == 0.0
    assert target_leakage({"f": [1.0, 2.0]}, [1.0, 2.0])["leak"] is False


def test_a_NON_monotone_leak_is_still_missed_and_that_is_STATED():
    """The honest residue, pinned so nobody reads this change as closing the class. `y**2` about a
    symmetric mean is the target and both coefficients read ~0; the rung that would see it has real
    false positives (a binary feature perfectly predicting a binary target is routine), so it needs
    a measurement before it can gate anything."""
    symmetric = [float(i) for i in range(-10, 11) if i != 0]
    squared = [v ** 2 for v in symmetric]
    assert target_leakage({"f": squared}, symmetric)["leak"] is False
