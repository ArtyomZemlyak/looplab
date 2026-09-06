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
