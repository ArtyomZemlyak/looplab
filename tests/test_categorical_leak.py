"""The leak the two coefficients cannot see, reported and NOT gated (doc 52 row 34).

`target_leakage` runs Pearson beside a tie-averaged rank coefficient, so a monotone re-encoding of
the target is caught and aborts the run. Two shapes read ~0 on both — `y**2` about a symmetric mean,
and a categorical id that maps to the label — and they are exactly what a grader-adjacent column
looks like in practice.

The property under test is the SPLIT: the rung sees them, and it does not gate. A statistic that
explains a target from its groups has real false positives a coefficient does not (a binary flag
against a binary label; any column with one row per group), and `target_leakage` aborts a run — so
what ships is a measurement, with the rate over real tasks left as its own open item.
"""
from __future__ import annotations

import pytest

from looplab.trust.leakage import (CATEGORICAL_LEAK_ADVISORY,
                                   CATEGORICAL_LEAK_BINNED_ADVISORY,
                                   categorical_leak, target_leakage)


def _y_squared(n=60):
    """A symmetric target and the feature it is a perfect FUNCTION of, with no monotone component."""
    xs = [float(i - n // 2) for i in range(n)]
    return {"x": xs}, [x * x for x in xs]


def _id_to_label(n=60, groups=6):
    """A categorical id whose every group maps to one label — the grader-adjacent column."""
    ids = [float(i % groups) for i in range(n)]
    return {"row_group": ids}, [float(int(v) % 2) for v in ids]


def test_the_coefficients_really_are_blind_to_these_two():
    """The precondition, driven rather than asserted from the marker: if either rung already flagged
    these, this whole detector would be redundant."""
    for features, target in (_y_squared(), _id_to_label()):
        verdict = target_leakage(features, target)
        assert verdict["leak"] is False, verdict
        for detail in verdict["flagged_detail"].values():
            assert abs(detail["pearson"]) < 0.9 and abs(detail["spearman"]) < 0.9


def test_a_non_monotone_functional_dependence_is_reported():
    """`y**2` is a PERFECT function of x with no monotone component. Its groups are singletons, so
    the rung bins x into equal-count quantile bins — and the binned statistic is a lower bound (the
    within-bin spread is thrown away), which is why the binned bar is not the coefficients'."""
    features, target = _y_squared()
    report = categorical_leak(features, target)
    assert report["x"]["rung"] == "eta_squared" and report["x"]["binned"] is True
    assert CATEGORICAL_LEAK_BINNED_ADVISORY <= report["x"]["score"] < CATEGORICAL_LEAK_ADVISORY
    assert report["x"]["rows_per_group"] == 5.0


def test_a_categorical_id_that_maps_to_the_label_is_reported():
    features, target = _id_to_label()
    report = categorical_leak(features, target)
    assert report["row_group"]["rung"] == "purity"
    assert report["row_group"]["score"] == pytest.approx(1.0)
    assert report["row_group"]["distinct"] == 6 and report["row_group"]["rows_per_group"] == 10.0


def test_a_column_with_one_row_per_group_is_not_a_finding():
    """Its RAW groups explain any target — arithmetic, not evidence — and after binning its bins
    carry mixed labels, so the same rung that sees `y**2` reads a row id as nothing. Both halves are
    needed: without the binning this is a false positive, without the continuous branch `y**2` is
    invisible."""
    n = 60
    features = {"row_id": [float(i) for i in range(n)]}
    target = [float(i % 3) for i in range(n)]
    assert categorical_leak(features, target) == {}


def test_a_column_the_coefficients_already_flagged_is_not_repeated():
    """The point of this rung is the RESIDUE. A plain linear relation is caught, aborts the run and
    has no business appearing again in an advisory a human is meant to read for what was MISSED."""
    xs = [float(i) for i in range(60)]
    verdict = target_leakage({"x": xs}, [2 * x + 1 for x in xs])
    assert verdict["leak"] is True and "x" in verdict["flagged"]
    assert verdict["categorical_advisory"] == {}


def test_the_routine_false_positive_is_reported_with_what_distinguishes_it():
    """A binary flag perfectly predicting a binary label is routine and legitimate. The rung cannot
    tell it from a leak — which is exactly why it does not gate — so it reports the two numbers a
    reader needs: how many groups, and how many rows in each."""
    n = 60
    flag = [float(i % 2) for i in range(n)]
    report = categorical_leak({"is_positive": flag}, list(flag))
    assert report["is_positive"]["score"] == pytest.approx(1.0)
    assert report["is_positive"]["distinct"] == 2
    assert report["is_positive"]["rows_per_group"] == 30.0


def test_an_imbalanced_target_does_not_make_every_column_a_finding():
    """RAW PURITY IS THE BASE RATE ON A SKEWED TARGET, so a rung that reported it would report
    every column of a rare-event table. Driven: at 98/2 an alternating flag and a three-valued
    group each predict the majority label for every row and score exactly 0.98 unrescaled — the
    base rate — while explaining nothing. Against the reducible error both are 0.0 and silent, and
    the column that actually determines the rare event is still 1.0."""
    target = [0.0] * 98 + [1.0, 1.0]
    assert categorical_leak({"flag": [float(i % 2) for i in range(100)]}, target) == {}
    assert categorical_leak({"g": [float(i % 3) for i in range(100)]}, target) == {}
    leaky = categorical_leak({"grader": list(target)}, target)
    assert leaky["grader"]["score"] == pytest.approx(1.0)
    assert leaky["grader"]["purity"] == pytest.approx(1.0)
    assert leaky["grader"]["base_rate"] == pytest.approx(0.98)


def test_the_row_says_both_numbers_so_a_reader_can_tell_the_two_apart():
    """A column at the bar on a BALANCED target and one at the bar on a skewed one are the same
    `score` and different findings; `purity` and `base_rate` beside it are what separates them."""
    balanced = [float(i % 2) for i in range(60)]
    row = categorical_leak({"is_positive": balanced}, list(balanced))["is_positive"]
    assert row["purity"] == pytest.approx(1.0) and row["base_rate"] == pytest.approx(0.5)


def test_a_short_table_or_a_constant_target_produces_nothing():
    assert categorical_leak({"x": [1.0, 2.0]}, [1.0, 2.0]) == {}
    assert categorical_leak({"x": [float(i % 4) for i in range(40)]}, [7.0] * 40) == {}
    assert categorical_leak({}, [1.0] * 40) == {}


def test_the_verdict_carries_it_as_advisory_and_never_as_a_leak():
    """THE SPLIT: the run must not abort on this rung. `leak` stays False and `flagged` stays empty
    while the advisory names the column."""
    features, target = _id_to_label()
    verdict = target_leakage(features, target)
    assert verdict["leak"] is False and verdict["flagged"] == {}
    assert "row_group" in verdict["categorical_advisory"]
    assert verdict["categorical_advisory"]["row_group"]["rung"] == "purity"


def test_nothing_in_the_engine_decides_on_the_advisory():
    """The measurement it needs before it may fire is a rate over real tasks; until then a reader
    that acts on it would be arming an unmeasured gate by the back door."""
    import ast

    from tests._source_scan import iter_trees

    readers = []
    for path, tree in iter_trees():
        if path.name == "leakage.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "categorical_advisory":
                readers.append(path.name)
            if isinstance(node, ast.Name) and node.id == "categorical_leak":
                readers.append(f"{path.name}: categorical_leak")
    assert not readers, f"something reads the advisory rung: {readers}"
