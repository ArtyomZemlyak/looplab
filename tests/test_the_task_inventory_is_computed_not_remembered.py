"""Which tasks can be scored, derived from artefacts rather than from a table somebody typed.

§309 ended with a sorted list of twenty tasks in a documentation table. A hand-typed list is exactly
what this bench keeps discovering it cannot trust: §291 found six comparison figures quoted from
probes that no longer exist, §300 found fifteen tasks with no data at all, §303 found nine rulers
that do not read unity. So the sorting is recomputed from the cache, the recorded readings and the
task source every time it is asked.

The rule, every number measured (§303-§309):
  * within 10 % of unity -> rules as is;
  * CP-SAT and off, with a LIGHT tail -> rules at one worker (1.4820 -> 1.0141, 1.2667 -> 1.0142,
    1.6028 -> 0.9974, -> 0.9871 measured);
  * CP-SAT and off, with a tail above p90/p10 = 30 -> unrulable under either regime (51.9 -> 1.145,
    45.2 -> 1.247, 32.2 -> 1.236, all still missing at one worker).

`discrete_log` is the control that keeps the rule honest: p90/p10 = 276, the heaviest tail on the
box, and it rules as it is because it is deterministic.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import ruler_check  # noqa: E402
import task_inventory as ti  # noqa: E402


def _rows(task, times, subset="test"):
    return [{"task": task, "subset": subset, "ok_name": True, "regime": "w22x1r3",
             "n": len(times), "times": times}]


LIGHT = [1.0] * 50 + [1.2] * 50           # p90/p10 = 1.2
HEAVY = [1.0] * 90 + [100.0] * 10         # p90/p10 = 100


def _cpsat(tmp_path, task, yes=True):
    d = tmp_path / task
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task}.py").write_text(
        "from ortools.sat.python import cp_model\n" if yes else "import networkx\n",
        encoding="utf-8")
    return str(tmp_path)


def _classify(tmp_path, task, times, reading, cpsat=True):
    old = ruler_check.CPSAT_ROOT
    try:
        ruler_check.CPSAT_ROOT = _cpsat(tmp_path, task, cpsat)
        return ti.classify(task, _rows(task, times), {task: (reading, "2026-09-06")})
    finally:
        ruler_check.CPSAT_ROOT = old


def test_a_reading_at_unity_rules_as_it_is(tmp_path):
    got = _classify(tmp_path, "t", LIGHT, 0.9797, cpsat=False)
    assert got["verdict"] == "rules as is", got


def test_a_deterministic_task_with_the_heaviest_tail_still_rules(tmp_path):
    """`discrete_log`: p90/p10 = 276 and reads 0.938. The tail alone decides nothing, and a rule
    that sorted on the tail would condemn the most stable task on the box."""
    got = _classify(tmp_path, "discrete_log", HEAVY, 0.9380, cpsat=False)
    assert got["verdict"] == "rules as is", got


def test_a_cpsat_task_with_a_light_tail_rules_at_one_worker(tmp_path):
    got = _classify(tmp_path, "t", LIGHT, 1.4820)
    assert got["verdict"] == "rules at one worker", got
    assert "contention at 22 workers" in got["why"], got


def test_a_cpsat_task_with_a_heavy_tail_rules_under_neither(tmp_path):
    got = _classify(tmp_path, "t", HEAVY, 1.8545)
    assert got["verdict"] == "unrulable", got
    assert "the speedup is a sum" in got["why"], got


def test_a_plain_task_that_is_off_is_not_filed_under_cpsat(tmp_path):
    """The distinction has to cut: a deterministic task reading 1.85 is §296's pagerank finding, and
    calling it a solver property would bury it."""
    got = _classify(tmp_path, "t", HEAVY, 1.8545, cpsat=False)
    assert got["verdict"] == "off by more than the tolerance", got


def test_a_task_with_no_reading_is_unread_not_scorable(tmp_path):
    old = ruler_check.CPSAT_ROOT
    try:
        ruler_check.CPSAT_ROOT = _cpsat(tmp_path, "t", False)
        got = ti.classify("t", _rows("t", LIGHT), {})
    finally:
        ruler_check.CPSAT_ROOT = old
    assert got["verdict"] == "unread" and "ruler_selfcheck --record" in got["why"], got


def test_a_task_with_no_baseline_says_so(tmp_path):
    old = ruler_check.CPSAT_ROOT
    try:
        ruler_check.CPSAT_ROOT = _cpsat(tmp_path, "t", False)
        got = ti.classify("t", [], {"t": (1.0, "2026-09-06")})
    finally:
        ruler_check.CPSAT_ROOT = old
    assert got["verdict"] == "no baseline", got


def test_the_task_list_comes_from_the_campaign_itself(tmp_path):
    """Typing the twenty names here would reintroduce exactly the hand-maintained list this file
    exists to avoid."""
    p = tmp_path / "campaign.sh"
    p.write_text('TASKS="${TASKS:-alpha beta \\\n gamma}"\n', encoding="utf-8")
    assert ti.campaign_tasks(p) == ["alpha", "beta", "gamma"]
    assert ti.campaign_tasks(tmp_path / "nope.sh") == []


def test_the_real_bench_sorts_into_the_measured_groups():
    """The end-to-end shape §309 recorded: sixteen of twenty scorable."""
    tasks = ti.campaign_tasks()
    if len(tasks) != 20:
        import pytest
        pytest.skip("campaign task list is not the twenty this was measured on")
    rows = ruler_check.entries(ruler_check.DEFAULT_DIR)
    out = [ti.classify(t, rows, ruler_check.latest_readings()) for t in tasks]
    counts = {}
    for r in out:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    assert counts.get("rules as is") == 10, counts
    assert counts.get("rules at one worker") == 6, counts
    assert counts.get("unrulable") == 3, counts


def test_a_reference_that_fails_its_own_checker_is_not_merely_unread(tmp_path):
    """§311: `spectral_clustering`'s own `is_solution` rejects 7 of its 100 reference solutions as
    `argmax over a k-column subset (suspicious)` -- an anti-reward-hack heuristic firing on the
    reference. 100 % validity is required, so nothing here can score it. Telling an operator to
    "run ruler_selfcheck --record" would send them to repeat a refusal."""
    old = ruler_check.CPSAT_ROOT
    try:
        ruler_check.CPSAT_ROOT = _cpsat(tmp_path, "spectral_clustering", False)
        got = ti.classify("spectral_clustering", _rows("spectral_clustering", LIGHT), {})
    finally:
        ruler_check.CPSAT_ROOT = old
    assert got["verdict"] == "unscorable reference", got
    assert "7 of its 100" in got["why"] and "§311" in got["why"], got
    assert "ruler_selfcheck --record" not in got["why"], got


def test_any_other_unread_task_is_still_just_unread(tmp_path):
    """The distinction has to cut both ways, or every unmeasured task inherits an excuse."""
    old = ruler_check.CPSAT_ROOT
    try:
        ruler_check.CPSAT_ROOT = _cpsat(tmp_path, "t", False)
        got = ti.classify("t", _rows("t", LIGHT), {})
    finally:
        ruler_check.CPSAT_ROOT = old
    assert got["verdict"] == "unread", got
    assert "ruler_selfcheck --record" in got["why"], got


def test_a_task_with_a_reading_is_never_called_unscorable(tmp_path):
    """The reference-invalid note must not outrank a measurement: if a reading exists, the reading
    decides."""
    old = ruler_check.CPSAT_ROOT
    try:
        ruler_check.CPSAT_ROOT = _cpsat(tmp_path, "spectral_clustering", False)
        got = ti.classify("spectral_clustering", _rows("spectral_clustering", LIGHT),
                          {"spectral_clustering": (0.99, "2026-09-06")})
    finally:
        ruler_check.CPSAT_ROOT = old
    assert got["verdict"] == "rules as is", got
