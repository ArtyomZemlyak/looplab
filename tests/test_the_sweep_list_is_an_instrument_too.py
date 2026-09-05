"""Five readings on the standing list are false, and one of them is backwards.

§219's lesson applied to the list itself: an instrument carrying false readings teaches its reader
to discount the true ones. Each sweep the same corrections got re-derived by hand and re-reported.

The one that is not merely stale: the money note says the abandoned `remDL` probe ($0.1292) must be
ADDED to the live sum "иначе получишь ложное расхождение". `remDL` has a tree on disk with 27
generation spans totalling $0.1292, so its money is already in the span sum -- following the
instruction manufactures the discrepancy it warns about.

These tests pin the CHECKS, not the verdicts. A claim that comes back true one day must be able to,
or the tool is a list of complaints rather than an instrument.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import sweep_claims  # noqa: E402


def _baselines(bench: Path, n: int, regime="w22x1r3"):
    d = bench / "looplab" / "benchmarks" / "algotune" / ".baseline_times"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"task{i}__test__{regime}.json").write_text(
            json.dumps({str(k): 1.0 + k for k in range(100)}), encoding="utf-8")


def _spans(bench: Path, name: str, costs):
    d = bench / "model-probes" / name / "runs" / "edge_expansion" / "run"
    d.mkdir(parents=True)
    (d / "spans.jsonl").write_text(
        "".join(json.dumps({"name": "generation", "attributes": {"cost": str(c)}}) + "\n"
                for c in costs), encoding="utf-8")


def test_the_baseline_claim_can_come_back_true(tmp_path):
    """Seven entries is what the list says. If the cache ever holds seven again, this must say so --
    otherwise the tool is a list of complaints, not a check."""
    _baselines(tmp_path, 7)
    ok, detail = sweep_claims.check_baseline_count(str(tmp_path))
    assert ok, detail


def test_nine_entries_is_reported_with_the_reason_the_count_is_not_the_invariant(tmp_path):
    _baselines(tmp_path, 9)
    ok, detail = sweep_claims.check_baseline_count(str(tmp_path))
    assert not ok
    assert "9 entries" in detail and "w22x1r3" in detail, detail
    assert "not the invariant" in detail, detail


def test_a_probe_with_a_tree_is_already_in_the_span_sum(tmp_path):
    """The backwards instruction, pinned: money that is on disk is money the reconciliation already
    counts, and adding it again is the error, not the fix."""
    _spans(tmp_path, "remDL", [0.1292])
    ok, detail = sweep_claims.check_abandoned_remdl(str(tmp_path))
    assert not ok, detail
    assert "0.1292" in detail and "MANUFACTURES" in detail, detail


def test_a_probe_with_no_tree_really_would_need_adding(tmp_path):
    """The claim is only wrong because the money is on disk. With nothing there the note would be
    right, and the check must be able to say so -- otherwise it tests the probe's NAME, not its
    money."""
    (tmp_path / "model-probes").mkdir(parents=True)
    ok, detail = sweep_claims.check_abandoned_remdl(str(tmp_path))
    assert ok, detail
    assert "no tree" in detail, detail


def test_a_tree_that_carries_no_billed_span_is_still_money_the_sum_lacks(tmp_path):
    """The case that tells "is there a directory" apart from "is the money in the sum". A tree can
    exist and carry nothing billed -- a probe that started and died before its first paid call --
    and then the reconciliation really is short by whatever the meter charged. Without this fixture
    a mutation swapping the two criteria survives, because every other fixture has them agreeing."""
    _spans(tmp_path, "remDL", [0.0, 0.0])
    ok, detail = sweep_claims.check_abandoned_remdl(str(tmp_path))
    assert ok, detail
    assert "a tree on disk" in detail and "$0.0000" in detail, detail


def test_the_running_probes_claim_reads_the_lanes(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep_claims.lanes, "probes",
                        lambda bench: [{"probe": n, "pid": 1, "cpus": set(), "argv": []}
                                       for n in ("remEE", "remDL2", "remPde")])
    ok, detail = sweep_claims.check_named_probes_running(str(tmp_path))
    assert ok, detail
    monkeypatch.setattr(sweep_claims.lanes, "probes",
                        lambda bench: [{"probe": "capA11", "pid": 1, "cpus": set(), "argv": []}])
    ok, detail = sweep_claims.check_named_probes_running(str(tmp_path))
    assert not ok and "capA11" in detail and "remEE" in detail, detail


def test_a_broken_check_is_not_a_verdict(tmp_path, monkeypatch, capsys):
    """A check that raises must not be silently counted as either true or stale: it is unchecked,
    and saying otherwise is exactly the false reading this tool exists to stop."""
    def boom(bench):
        raise RuntimeError("no such thing")
    monkeypatch.setattr(sweep_claims, "CLAIMS", [("a claim", boom)])
    rc = sweep_claims.main(["--bench", str(tmp_path)])
    out = capsys.readouterr().out
    assert "UNCHECKABLE" in out and "no such thing" in out, out
    assert rc == 0 and "0 of 1" in out, (rc, out)


def test_the_comparison_figure_is_read_from_the_probes_own_record(tmp_path):
    """§73.4 settled this on 2026-09-01 and the list still carries the old number: accEE's TEST is
    224.8846, measured here with its Cython kernel building. A comparison figure 0.20 % below the
    truth makes every future probe on this task look slightly better than it is."""
    d = tmp_path / "model-probes" / "accEE"
    d.mkdir(parents=True)
    (d / "final.json").write_text(json.dumps({"speedup": 224.8846, "subset": "test"}),
                                  encoding="utf-8")
    ok, detail = sweep_claims.check_accee_test(str(tmp_path))
    assert not ok and "224.8846" in detail and "73.4" in detail, detail


def test_the_figure_would_hold_if_the_record_said_so(tmp_path):
    d = tmp_path / "model-probes" / "accEE"
    d.mkdir(parents=True)
    (d / "final.json").write_text(json.dumps({"speedup": 224.4432}), encoding="utf-8")
    ok, detail = sweep_claims.check_accee_test(str(tmp_path))
    assert ok, detail


def test_an_unreadable_record_is_not_a_verdict_either_way(tmp_path):
    (tmp_path / "model-probes").mkdir(parents=True)
    ok, detail = sweep_claims.check_accee_test(str(tmp_path))
    assert not ok and "cannot read" in detail, detail
