"""Two readings of the same task in different evaluation regimes are two different questions.

§314. `max_clique_cpsat` reads 1.5291 at twenty-two evaluation workers and 0.9922 at one, on the
same idle box, against baselines built in each regime. Re-timing the baseline quiet moved it 2 %,
so contention is not the cause; the asymmetry is between the baseline pass and the candidate pass
and only exists at twenty-two workers.

Before this, a reading recorded the lane and the load but not the regime, and the inventory called
six CP-SAT tasks "rules at one worker" without a one-worker reading anywhere on the box -- an
inference from "CP-SAT and a light tail" printed in the same column as measured numbers.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import ruler_check  # noqa: E402
import ruler_selfcheck  # noqa: E402
import task_inventory  # noqa: E402


def _log(tmp, rows):
    path = Path(tmp) / "readings.jsonl"
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    return path


def test_the_regime_recorded_is_the_one_on_disk_not_the_one_asked_for(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "cache"
        cache.mkdir()
        # The run ASKED for one worker; what the harness wrote is the twenty-two-wide key. §305 is
        # exactly this, and recording the request would have called that run serial.
        (cache / "max_clique_cpsat__test__w22x1r3.json").write_text("{}", encoding="utf-8")
        (cache / "max_clique_cpsat__test__w22x1r3.json.provenance.json").write_text(
            "{}", encoding="utf-8")
        monkeypatch.setenv("ALGOTUNE_BASELINE_CACHE_DIR", str(cache))
        # THE INTENT IS SET AND IT DISAGREES WITH THE DISK. Without this line the fixture passes on
        # a version that reports the requested regime, because request and reality coincide.
        monkeypatch.setenv("ALGOTUNE_EVAL_WORKERS", "1")
        assert ruler_selfcheck.observed_regime("max_clique_cpsat", "test") == "w22x1r3"


def test_the_row_carries_the_regime():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "readings.jsonl"
        ruler_selfcheck.append_reading(log, "max_clique_cpsat", "test", [0.99], 0.99,
                                       stamp="2026-09-06T18:00:00", lane="33-43,81-91", busy=0,
                                       regime="lane22r3")
        row = json.loads(log.read_text(encoding="utf-8").strip())
        assert row["regime"] == "lane22r3"


def test_latest_readings_does_not_mix_the_regimes():
    with tempfile.TemporaryDirectory() as tmp:
        path = _log(tmp, [
            {"task": "max_clique_cpsat", "median": 1.5291, "stamp": "2026-09-06T16:00:00",
             "regime": "w22x1r3"},
            # LATER IN TIME, and a different question. Newest-wins without a regime filter answers
            # "how does it rule at twenty-two?" with a number measured at one.
            {"task": "max_clique_cpsat", "median": 0.9922, "stamp": "2026-09-06T17:00:00",
             "regime": "lane22r3"},
        ])
        wide = ruler_check.latest_readings(path, regime="w22x1r3")
        serial = ruler_check.latest_readings(path, regime="lane22r3")
        assert round(wide["max_clique_cpsat"][0], 4) == 1.5291
        assert round(serial["max_clique_cpsat"][0], 4) == 0.9922


def test_rules_at_one_worker_is_not_claimed_without_a_one_worker_reading():
    rows = [{"task": "max_clique_cpsat", "subset": "test",
             "times": [10.0] * 50 + [130.0] * 50}]
    readings = {"max_clique_cpsat": (1.6028, "2026-09-06T06:51:26")}

    # THE FIXTURE THAT DISAGREES WITH THE BUG: everything the old inference used is present --
    # CP-SAT, a light tail, a reading above tolerance -- and the one thing it never had is absent.
    got = task_inventory.classify("max_clique_cpsat", rows, readings, serial={})
    assert got["verdict"] == "unread at one worker", got

    seen = task_inventory.classify("max_clique_cpsat", rows, readings,
                                   serial={"max_clique_cpsat": (0.9922, "2026-09-06T17:40:00")})
    assert seen["verdict"] == "rules at one worker" and round(seen["serial"], 4) == 0.9922
    assert "concurrency, not the solver" in seen["why"]


def test_a_serial_reading_that_does_not_read_unity_does_not_rescue_the_task():
    """Existence is not a verdict. Measured the same evening, both serial and both on an idle box:
    max_clique_cpsat read 0.9922 in one sitting and 1.0967 in the next. A check that accepted any
    serial row would price a candidate against whichever sitting happened to be last."""
    rows = [{"task": "min_dominating_set", "subset": "test",
             "times": [10.0] * 50 + [130.0] * 50}]
    readings = {"min_dominating_set": (1.3175, "2026-09-06T06:40:00")}
    got = task_inventory.classify("min_dominating_set", rows, readings,
                                  serial={"min_dominating_set": (1.2500, "2026-09-06T18:00:00")})
    assert got["verdict"] == "unrulable", got
    assert "serial removes most of the excess" in got["why"]


def test_a_row_written_before_the_regime_existed_counts_only_for_the_wide_one():
    """Twenty rows were recorded before the key existed, all of them twenty-two wide. Dropping them
    reported a box that had read twenty tasks as having read four; accepting them into the SERIAL
    slot would let a twenty-two-wide number prove one-worker rulability, which is the whole claim."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _log(tmp, [{"task": "kcenters", "median": 0.9021, "stamp": "2026-09-06T06:53:22"}])
        wide = ruler_check.latest_readings(path, regime="w22x1r3", accept_unstamped=True)
        assert round(wide["kcenters"][0], 4) == 0.9021
        strict = ruler_check.latest_readings(path, regime="w22x1r3")
        assert "kcenters" not in strict
        serial = ruler_check.latest_readings(path, regime="lane22r3", accept_unstamped=True)
        assert "kcenters" not in serial, "an unstamped row is not a one-worker reading"


def test_the_tail_comes_from_the_wide_entry_not_whichever_file_sorted_first():
    """The hour both regimes existed in the cache, `max_common_subgraph`'s tail fell from 15.0 to
    1.4 with nothing measured: the entry lookup was first-match-wins and `lane22r3` sorts before
    `w22x1r3`. The fixture keeps BOTH entries with visibly different spreads, so a regime-blind
    lookup cannot pass by accident."""
    rows = [
        {"task": "max_common_subgraph", "subset": "test", "regime": "lane22r3",
         "times": [10.0] * 100},                                   # flat: tail 1.0
        {"task": "max_common_subgraph", "subset": "test", "regime": "w22x1r3",
         "times": [10.0] * 50 + [150.0] * 50},                     # tail 15.0
    ]
    readings = {"max_common_subgraph": (1.4820, "2026-09-06T06:43:55")}
    got = task_inventory.classify("max_common_subgraph", rows, readings,
                                  serial={"max_common_subgraph": (1.0113, "2026-09-06T18:00:00")})
    assert round(got["tail"], 1) == 15.0, got
