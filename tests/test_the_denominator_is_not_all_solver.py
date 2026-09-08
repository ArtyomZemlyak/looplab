"""Every speedup on this box divides by a cached per-instance time, and that number is not the
reference solving anything.

Measured 2026-09-07 at the dataset's own instance size, under the bench interpreter, three
generated instances and three repeats, warm:

    task             cached per-instance   in-process   overhead
    pde_heat1d            146.5 ms           75.4 ms       49 %
    edge_expansion         45.4 ms           30.4 ms       33 %

Isolation, warmups and validation are what make the cached number reproducible, so this is not a
defect. It is the denominator of every score here, and it had never been separated into its parts:
a reader comparing a candidate against "the reference's time" was comparing it against roughly twice
that for `pde_heat1d`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import ruler_selfcheck  # noqa: E402


def test_the_dataset_size_is_read_off_the_name(tmp_path):
    d = tmp_path / ".hf_datasets" / "x" / "data" / "pde_heat1d"
    d.mkdir(parents=True)
    (d / "pde_heat1d_T100ms_n8_size100_test.jsonl").write_text("", encoding="utf-8")
    assert ruler_selfcheck.dataset_n("pde_heat1d", root=str(tmp_path)) == 8


def test_the_size_is_not_confused_with_the_other_numbers_in_the_name(tmp_path):
    """`_T100ms_`, `_n8_` and `_size100_` all carry integers, and the target-time parser lives one
    function away. A regex loose enough to take the first number would return 100."""
    d = tmp_path / ".hf_datasets" / "x" / "data" / "pde_heat1d"
    d.mkdir(parents=True)
    (d / "pde_heat1d_T100ms_n8_size100_test.jsonl").write_text("", encoding="utf-8")
    assert ruler_selfcheck.dataset_n("pde_heat1d", root=str(tmp_path)) == 8
    assert ruler_selfcheck.dataset_target_ms("pde_heat1d", root=str(tmp_path)) == 100.0


def test_the_in_process_timing_runs_under_the_bench_interpreter():
    """§299 cost a week to a number timed under the wrong Python: `/opt/conda/bin/python` reads
    pagerank at 74.6 ms where the venv that SCORES reads 110.0. A helper whose whole purpose is to
    compare against the harness's number may not be timed by a different interpreter."""
    src = (BENCH / "ruler_selfcheck.py").read_text(encoding="utf-8")
    body = src[src.index("def in_process_ms("):src.index("def _cached_median_ms(")]
    assert "bench_python()" in body, body
    assert "sys.executable" not in body


def test_the_line_states_the_overhead_and_not_the_cached_number(tmp_path, capsys):
    """The failure this guards is quiet: printing the cached median as if it were solver time. The
    fixture pins the arithmetic -- 146.5 against 75.4 is 49 %, not 146 and not 51."""
    src = (BENCH / "ruler_selfcheck.py").read_text(encoding="utf-8")
    line = [ln for ln in src.splitlines() if "of the denominator is harness overhead" in ln]
    assert line, "the line is not printed at all"
    body = src[src.index("n_size = dataset_n(args.task)"):src.index("of the denominator is harness overhead")]
    assert "100 * (cached - direct) / cached" in src, "the percentage is not the overhead share"
    # And it is only printed when the in-process timing is the SMALLER of the two: a direct timing
    # above the cached one means the comparison failed (wrong size, cold cache), not that the
    # harness has negative overhead.
    assert "direct < cached" in body, body


def test_the_reading_records_both_halves(tmp_path):
    """Recorded, not recomputed: a sweep that had to re-time the solver half would cost a minute a
    task, and the alternative it fell back to before was quoting the number from a comment."""
    import json

    log = tmp_path / "readings.jsonl"
    ruler_selfcheck.append_reading(log, "pde_heat1d", "test", [1.0], 1.0,
                                   stamp="2026-09-07T10:00:00", lane="33-43,81-91", busy=0,
                                   regime="w22x1r3", solver_ms=75.4, cached_ms=146.5)
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["solver_ms"] == 75.4 and row["cached_ms"] == 146.5
    # A reading taken before the fields existed says None rather than zero: an absent measurement
    # and a measured zero would otherwise both read as "no overhead".
    ruler_selfcheck.append_reading(log, "pde_heat1d", "test", [1.0], 1.0,
                                   stamp="2026-09-07T10:00:01", lane=None)
    second = json.loads(log.read_text(encoding="utf-8").splitlines()[1])
    assert second["solver_ms"] is None and second["cached_ms"] is None
