"""Point 5 carries four numbers the sweep never checked, and point 2 says how a check can lie.

`ruler_check.py` verifies the SHAPE of the baseline cache — one regime, a full set of per-instance
timings. It says nothing about the READING: submit the reference itself and `speedup` must come back
~1.0, because both sides of `baseline_ms / optimized_ms` are then the same code. `baseline_ms` comes
from the cache (written once) and `optimized_ms` is timed now, so the self-speedup is the ratio of
"how fast this box was then" to "how fast it is today".

Measured 2026-09-04: `edge_expansion` 0.8861 against the sweep's 0.9847 (−10.0 %), four repeats
inside 0.875–0.899, and three more with the other lanes idle at 0.8865 — not load, drift.

The two ways this reading is not a reading are both in point 2's own words: a zero that arrives in a
second is the harness declining, not a slow solver. Both were hit while building the tool
(`solver_unloadable`, then `Task data directory not found`), and both had `eval_seconds` ~1.7
against a real ~28 s.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import ruler_selfcheck  # noqa: E402


def test_a_zero_that_arrives_in_a_second_is_a_refusal():
    assert ruler_selfcheck.refused({"speedup": 0.0, "eval_seconds": 1.7})
    assert ruler_selfcheck.refused({"speedup": 0.0, "eval_seconds": 0.1})


def test_a_named_refusal_is_named_even_when_it_looks_like_a_score():
    """`solver_unloadable` came back with a plausible-looking envelope; the reason field is the
    claim and it outranks the number."""
    why = ruler_selfcheck.refused(
        {"speedup": 0.0, "eval_seconds": 1.7,
         "no_speedup": {"reason": "solver_unloadable",
                        "evaluator_verdict": "No module named 'reference_edge_expansion'"}})
    assert "solver_unloadable" in why


def test_a_real_zero_from_a_real_evaluation_is_not_called_a_refusal():
    """A solver that genuinely fails validity scores 0.0 after a FULL evaluation — arm A's
    `pagerank` did, at 0.0 with 66 verification failures (§181). Calling that a harness refusal
    would erase a real result."""
    assert ruler_selfcheck.refused({"speedup": 0.0, "eval_seconds": 41.2}) == ""


def test_an_ordinary_reading_is_not_a_refusal():
    assert ruler_selfcheck.refused({"speedup": 0.8861, "eval_seconds": 27.6}) == ""


def test_the_solver_it_builds_is_self_contained(tmp_path):
    """`--solver-file-only` copies ONE file. The first version imported the reference module and
    every evaluation came back `solver_unloadable` in 1.7 s."""
    ref = tmp_path / "probes" / "p1" / "ws" / "edge_expansion"
    ref.mkdir(parents=True)
    (ref / "reference_edge_expansion.py").write_text(
        "class Task:\n    pass\n\n\n"
        "class EdgeExpansionTask(Task):\n"
        "    def solve(self, problem):\n        return {'edge_expansion': 1.0}\n",
        encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    path = ruler_selfcheck.build_solver("edge_expansion", str(out), str(tmp_path / "probes"))
    body = Path(path).read_text(encoding="utf-8")
    assert "import reference_edge_expansion" not in body and "from reference_" not in body, (
        "the reference is imported, not inlined; --solver-file-only leaves it behind")
    assert "class EdgeExpansionTask(Task)" in body, "the reference implementation is not in the file"
    ns: dict = {}
    exec(compile(body, path, "exec"), ns)                      # noqa: S102 - that IS the check
    assert ns["Solver"]().solve({"x": 1}) == {"edge_expansion": 1.0}, (
        "the built Solver does not delegate to the reference's own solve()")


def test_a_task_with_no_delivered_reference_is_an_error_not_a_number(tmp_path):
    try:
        ruler_selfcheck.build_solver("no_such_task", str(tmp_path), str(tmp_path))
    except FileNotFoundError:
        return
    raise AssertionError("a missing reference must refuse, not build an empty solver")


def test_the_instance_share_says_why_eval_seconds_cannot_see_the_drift(tmp_path):
    """A hundred instances are a minority of an evaluation's wall clock, and not the same minority
    per task: 10.9 % for `edge_expansion`, 63 % for `pde_heat1d`. That arithmetic is why a flat
    `eval_seconds` does not refute a self-speedup of 0.8861."""
    import json
    (tmp_path / "t__test__w22x1r3.json").write_text(
        json.dumps({str(i): 45.0 for i in range(100)}), encoding="utf-8")
    got = ruler_selfcheck.instance_share("t", 41.0, times_dir=tmp_path)
    assert abs(got - 4.5 / 41.0) < 1e-6, got
    # A task whose instances dominate reads high, and the same code must say so.
    (tmp_path / "u__test__w22x1r3.json").write_text(
        json.dumps({str(i): 145.0 for i in range(100)}), encoding="utf-8")
    assert ruler_selfcheck.instance_share("u", 23.0, times_dir=tmp_path) > 0.6


def test_a_missing_or_zero_denominator_is_zero_not_a_crash(tmp_path):
    assert ruler_selfcheck.instance_share("nope", 41.0, times_dir=tmp_path) == 0.0
    import json
    (tmp_path / "t__test__w22x1r3.json").write_text(
        json.dumps({"0": 45.0}), encoding="utf-8")
    assert ruler_selfcheck.instance_share("t", 0.0, times_dir=tmp_path) == 0.0


def test_a_reading_is_appended_as_a_dated_row(tmp_path):
    """One number cannot say WHEN the cached baseline and the box parted; a series can. §215 closed
    off the obvious proxy — `eval_seconds` times a different solver every node — so a fixed-work
    reading taken every sweep is the only instrument left, and it has to start somewhere."""
    log = tmp_path / "series.jsonl"
    ruler_selfcheck.append_reading(log, "edge_expansion", "test",
                                   [0.8849, 0.8872, 0.8994], 0.8872, stamp="2026-09-04T13:00:00")
    ruler_selfcheck.append_reading(log, "edge_expansion", "test",
                                   [0.8908, 0.8912, 0.8833], 0.8908, stamp="2026-09-04T15:20:00")
    ruler_selfcheck.append_reading(log, "discrete_log", "test", [1.09], 1.09,
                                   stamp="2026-09-04T13:50:00")
    # OUT OF APPEND ORDER, which is the reason the reader sorts at all: the file's order is the
    # order rows were WRITTEN, and a sweep records several tasks in whatever order their lanes
    # finish -- or back-fills a reading it took earlier. Mutation showed a fixture appended in
    # time order cannot tell a sorted reader from an unsorted one.
    ruler_selfcheck.append_reading(log, "edge_expansion", "test", [0.9002], 0.9002,
                                   stamp="2026-09-03T09:00:00")
    got = ruler_selfcheck.read_series(log, "edge_expansion")
    assert [r["median"] for r in got] == [0.9002, 0.8872, 0.8908], got
    assert [r["stamp"] for r in got] == ["2026-09-03T09:00:00", "2026-09-04T13:00:00",
                                        "2026-09-04T15:20:00"], (
        "the series is not in time order, so a drift cannot be read off it")
    assert len(ruler_selfcheck.read_series(log)) == 4, "the task filter dropped other tasks entirely"


def test_the_series_survives_a_torn_line(tmp_path):
    """Appended under a crash-prone box: half a row must cost one reading, not the series."""
    log = tmp_path / "series.jsonl"
    ruler_selfcheck.append_reading(log, "t", "test", [1.0], 1.0, stamp="2026-09-04T10:00:00")
    with open(log, "a", encoding="utf-8") as fh:
        fh.write('{"stamp": "2026-09-04T11:00:00", "med')
    ruler_selfcheck.append_reading(log, "t", "test", [1.1], 1.1, stamp="2026-09-04T12:00:00")
    assert [r["median"] for r in ruler_selfcheck.read_series(log, "t")] == [1.0, 1.1]
    assert ruler_selfcheck.read_series(tmp_path / "nope.jsonl") == []


def test_the_caller_owns_the_clock(tmp_path):
    """A row stamped by the reader replays differently every time it is read; the stamp is data."""
    log = tmp_path / "series.jsonl"
    row = ruler_selfcheck.append_reading(log, "t", "test", [1.0], 1.0, stamp="2026-01-01T00:00:00")
    assert row["stamp"] == "2026-01-01T00:00:00"
    auto = ruler_selfcheck.append_reading(log, "t", "test", [1.0], 1.0)
    assert auto["stamp"] and auto["stamp"] != "2026-01-01T00:00:00"


def test_a_refusal_carries_the_evaluator_s_own_explanation():
    """`REFUSED: baseline_regime_mismatch` four times over says nothing an operator can act on. The
    evaluator names both keys and the consequence, and that sentence is the whole diagnosis.

    Measured 2026-09-05 by running the self-check on the SERVICE lanes: the regime key encodes the
    lane WIDTH, so eight cpus key `__w8x1r3` where the cache holds `__w22x1r3`, and §149's guard
    refuses rather than silently re-timing the reference against a different denominator."""
    row = {"speedup": None, "eval_seconds": 0.0, "no_speedup": {
        "reason": "baseline_regime_mismatch",
        "detail": "this invocation would key its baseline '__w8x1r3', which is not on disk, while "
                  "edge_expansion__test__w22x1r3.json already is"}}
    why = ruler_selfcheck.refused(row)
    assert "baseline_regime_mismatch" in why
    assert "__w8x1r3" in why and "__w22x1r3" in why, (
        f"{why!r}: the label survived and the explanation did not")


def test_a_refusal_with_no_detail_still_names_itself():
    why = ruler_selfcheck.refused({"speedup": None, "eval_seconds": 0.0,
                                   "no_speedup": {"reason": "solver_unloadable"}})
    assert why == "solver_unloadable", why


def test_the_default_lane_is_a_bench_lane_not_the_service_lanes():
    """§259 says pin every analysis to 44-47,92-95; this is the one measurement that cannot obey,
    because the regime key it must match is defined by a 22-cpu lane."""
    import re
    src = (Path(__file__).resolve().parents[1] / "benchmarks" / "ruler_selfcheck.py").read_text(
        encoding="utf-8")
    got = re.search(r'"--lane",\s*default="([^"]+)"', src)
    assert got, "the --lane default vanished"
    width = sum(int(b) - int(a) + 1 if "-" in part else 1
                for part in got.group(1).split(",")
                for a, b in [part.split("-") if "-" in part else (part, part)])
    assert width == 22, f"default lane {got.group(1)!r} is {width} cpus; the cached regime is w22"
