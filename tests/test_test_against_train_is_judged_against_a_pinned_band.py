"""A band derived from the probes it judges cannot be failed by them.

Point 9 asks for TEST against TRAIN per probe. Measured 2026-09-08 over the 141 probes with both:
edge_expansion x0.995 (0.892-1.033, n=118), discrete_log x1.001 (0.890-1.260, n=11), pde_heat1d
x1.023 (0.991-1.054, n=10) -- the spread is the task's own tail, `discrete_log`'s cached times
running p90/p10 = 276.

The first cut of the check recomputed each band from the corpus and then asked whether the corpus
sat inside it. It reported HOLDS on its own tautology -- the shape this file exists to catch,
written into this file. The bands are pinned now, so a FUTURE probe can fall outside one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402

STUB = '''#!/usr/bin/env python3
import pathlib
print(pathlib.Path(__file__).with_name("rows.json").read_text())
'''


def _bench(tmp_path, rows) -> str:
    tools = tmp_path / "looplab" / "benchmarks"
    tools.mkdir(parents=True)
    (tools / "probe_summary.py").write_text(STUB, encoding="utf-8")
    (tools / "rows.json").write_text(json.dumps(rows), encoding="utf-8")
    return str(tmp_path)


def _row(probe, task, best, test):
    return {"probe": probe, "task": task, "nodes": [best], "test": test}


def test_a_probe_inside_the_pinned_band_passes(tmp_path):
    rows = [_row("a", "edge_expansion", 100.0, 99.5), _row("b", "edge_expansion", 100.0, 100.0)]
    ok, detail = sweep_claims.check_test_tracks_train(_bench(tmp_path, rows))
    assert ok, detail


def test_a_probe_outside_the_pinned_band_is_named(tmp_path):
    """THE CASE THE FIRST CUT COULD NOT PRODUCE. x1.400 on edge_expansion is far outside
    0.892-1.033, and a band recomputed from these two rows would have contained it."""
    rows = [_row("a", "edge_expansion", 100.0, 140.0), _row("b", "edge_expansion", 100.0, 100.0)]
    ok, detail = sweep_claims.check_test_tracks_train(_bench(tmp_path, rows))
    assert not ok, detail
    assert "OUTSIDE the pinned band: a on edge_expansion x1.400 outside 0.892-1.033" in detail, detail


def test_a_task_with_no_pinned_band_is_reported_rather_than_passed(tmp_path):
    rows = [_row("a", "kcenters", 10.0, 10.5), _row("b", "kcenters", 10.0, 9.8)]
    ok, detail = sweep_claims.check_test_tracks_train(_bench(tmp_path, rows))
    assert not ok, detail
    assert "UNPINNED task(s): kcenters" in detail, detail


def test_one_probe_is_a_point_and_is_not_judged(tmp_path):
    """Pinning `pagerank`'s single x0.993 as a band flagged that very probe on the next run: 0.99296
    is outside 0.993-0.993 by rounding alone. A band needs two, and saying so is cheaper than
    inventing a tolerance nobody measured.

    §376 pinned `pagerank` at ten probes, so the fixture moved to a task the bands do NOT cover: the
    property is "a task with one probe is not judged", and it must be exercised on a task that is
    actually unpinned or it stops testing anything. The pagerank story stays because it is the
    reason the property exists."""
    rows = [_row("pgr1", "kcenters", 100.0, 99.296)]
    ok, detail = sweep_claims.check_test_tracks_train(_bench(tmp_path, rows))
    assert ok, detail
    assert "too few probes for a band: kcenters" in detail, detail


def test_the_bands_are_pinned_with_the_count_they_were_measured_over():
    for task, band in sweep_claims.TEST_TRAIN_BANDS.items():
        lo, hi, n = band
        assert n >= sweep_claims.MIN_PROBES_FOR_A_BAND, (task, band)
        assert lo <= hi, (task, band)
