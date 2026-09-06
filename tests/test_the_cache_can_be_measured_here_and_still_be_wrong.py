""""Все перемерены ЗДЕСЬ" is what the standing sweep says of every baseline entry. It is true, and
it is not the same as still being right.

Measured 2026-09-06 by timing all four references into a scratch cache and dividing by the cached
medians, beside the reference-against-itself readings taken separately:

    task             cached/fresh   self-check reading
    edge_expansion       0.868          0.9007
    pde_heat1d           1.016          1.0676
    discrete_log         1.052          1.0830
    pagerank             1.463          1.4317

Two independent methods -- one re-times the reference, the other runs it as a candidate against the
cache -- agreeing to within 0.04 on every task. So the reading measures how wrong the cache is, and
`pagerank`'s is high by 46 % while the other three sit within 13 %. Every score on a task divides by
its cached baseline, so a 46 % error is a 46 % error in every number that task ever produced.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import ruler_check  # noqa: E402


def _log(tmp_path: Path, rows):
    p = tmp_path / "series.jsonl"
    p.write_text("".join(json.dumps({"task": t, "median": m, "stamp": s}) + "\n"
                         for t, m, s in rows), encoding="utf-8")
    return p


def _rows(*tasks):
    return [{"task": t, "ok_name": True, "regime": "w22x1r3", "n": 100} for t in tasks]


def test_a_cache_its_own_check_calls_wrong_is_named(tmp_path):
    readings = ruler_check.latest_readings(_log(tmp_path, [("pagerank", 1.4317, "2026-09-06")]))
    said = ruler_check.stale_entries(_rows("pagerank"), readings)
    assert said and "high by 43 %" in said[0], said
    assert "every score on this task divides by it" in said[0], said


def test_a_cache_that_is_low_is_named_as_low(tmp_path):
    """The direction matters: a low cache inflates every score on the task, a high one deflates it,
    and "off by 30 %" without a direction tells the reader nothing to act on."""
    readings = ruler_check.latest_readings(_log(tmp_path, [("edge_expansion", 0.70, "2026-09-06")]))
    said = ruler_check.stale_entries(_rows("edge_expansion"), readings)
    assert said and "low by 30 %" in said[0], said


def test_the_three_ordinary_tasks_stay_quiet(tmp_path):
    """0.9007, 1.0676 and 1.0830 are the real readings for the other three, and none of them is
    worth an alarm every sweep. A threshold that fires on all four teaches its reader to skip it."""
    readings = ruler_check.latest_readings(_log(tmp_path, [
        ("edge_expansion", 0.9007, "2026-09-06"), ("pde_heat1d", 1.0676, "2026-09-05"),
        ("discrete_log", 1.0830, "2026-09-05")]))
    rows = _rows("edge_expansion", "pde_heat1d", "discrete_log")
    assert ruler_check.stale_entries(rows, readings) == []


def test_the_latest_reading_wins(tmp_path):
    readings = ruler_check.latest_readings(_log(tmp_path, [
        ("pagerank", 1.0000, "2026-09-01"), ("pagerank", 1.4317, "2026-09-06")]))
    assert readings["pagerank"][0] == 1.4317


def test_a_reading_for_a_task_with_no_cache_entry_is_not_reported(tmp_path):
    """The check is about entries that exist; a reading for a task this cache does not hold is
    somebody else's problem and reporting it here is noise."""
    readings = ruler_check.latest_readings(_log(tmp_path, [("nosuchtask", 2.0, "2026-09-06")]))
    assert ruler_check.stale_entries(_rows("pagerank"), readings) == []


def test_a_missing_log_is_not_an_all_clear_by_accident(tmp_path):
    """An empty series means nothing has been checked, which must not read as everything is fine --
    it reads as no entries reported, and `ruler_check`'s other checks still run."""
    assert ruler_check.latest_readings(tmp_path / "nope.jsonl") == {}


def _task_src(tmp_path: Path, task: str, body: str):
    d = tmp_path / task
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task}.py").write_text(body, encoding="utf-8")
    return str(tmp_path)


def test_a_cpsat_task_is_named_as_unrulable_not_as_a_drifting_cache(tmp_path):
    """Measured 2026-09-06 across all 19 tasks that returned a number, three repeats each: the nine
    CP-SAT tasks read 1.1375-1.8545 and the ten others 0.9021-1.0670, with NO overlap. CP-SAT is
    multi-threaded and nondeterministic -- within one task the repeats swing 1.72/2.09/1.85, against
    0.96/0.98/0.98 for edge_expansion. A 46 % reading on pagerank was worth a week (§292-§299); the
    same number here means only that CP-SAT was asked twice."""
    root = _task_src(tmp_path, "min_dominating_set",
                     "from ortools.sat.python import cp_model\n")
    said = ruler_check.stale_entries(
        _rows("min_dominating_set"),
        {"min_dominating_set": (1.8627, "2026-09-06")})
    old = ruler_check.CPSAT_ROOT
    try:
        ruler_check.CPSAT_ROOT = root
        said = ruler_check.stale_entries(_rows("min_dominating_set"),
                                         {"min_dominating_set": (1.8627, "2026-09-06")})
    finally:
        ruler_check.CPSAT_ROOT = old
    assert said and "drifting cache" in said[0], said
    # THE MECHANISM, NOT JUST THE LABEL. §303 said "nondeterministic" and §304 measured that the
    # dominant cause is allocation: x2.2 between one core and a lane, against a per-instance repeat
    # spread of only x1.3 which averages away over a hundred instances. A reader told "the solver is
    # noisy" would reach for more repeats, which is precisely what does not help.
    assert "x2.2" in said[0] and "num_search_workers" in said[0], said[0]
    assert "not mere noise" in said[0], said[0]
    assert "Fixable" in said[0], said[0]


def test_a_plain_task_with_the_same_reading_is_still_a_drifting_cache(tmp_path):
    """The distinction has to cut: an identical number on a deterministic task is the pagerank
    finding, and calling it 'nondeterministic solver' would have buried §296."""
    root = _task_src(tmp_path, "pagerank", "import networkx\n")
    old = ruler_check.CPSAT_ROOT
    try:
        ruler_check.CPSAT_ROOT = root
        said = ruler_check.stale_entries(_rows("pagerank"), {"pagerank": (1.8627, "2026-09-06")})
    finally:
        ruler_check.CPSAT_ROOT = old
    assert said and "cached baseline is high" in said[0], said
    assert "CP-SAT" not in said[0], said


def test_a_cpsat_task_reading_one_is_not_reported_at_all(tmp_path):
    """Being unrulable in principle is not a reason to shout when the reading happens to be fine."""
    root = _task_src(tmp_path, "kcenters", "from ortools.sat.python import cp_model\n")
    old = ruler_check.CPSAT_ROOT
    try:
        ruler_check.CPSAT_ROOT = root
        assert ruler_check.stale_entries(_rows("kcenters"),
                                         {"kcenters": (1.0, "2026-09-06")}) == []
    finally:
        ruler_check.CPSAT_ROOT = old


def test_an_unreadable_task_source_is_not_assumed_to_be_cpsat(tmp_path):
    """Guessing CP-SAT for a task we cannot read would silence a real drifting cache."""
    old = ruler_check.CPSAT_ROOT
    try:
        ruler_check.CPSAT_ROOT = str(tmp_path / "nothing-here")
        assert ruler_check.uses_cpsat("whatever") is False
    finally:
        ruler_check.CPSAT_ROOT = old
