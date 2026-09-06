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


def _make_task(tmp_path: Path, text: str):
    d = tmp_path / "looplab" / "benchmarks" / "algotune"
    d.mkdir(parents=True, exist_ok=True)
    (d / "make_task.py").write_text(text, encoding="utf-8")


def test_item_a_is_shipped_and_the_list_still_calls_it_missing(tmp_path):
    """Read out of the generated card on 2026-09-05: the `goal` field carries the ceiling, the
    arithmetic `(1 + 5) * reference_time * 10`, and the consequence that a killed instance is
    INVALID rather than slow. Re-shipping it after the readout would have been wasted work."""
    _make_task(tmp_path, "x = '(1 + 5) * reference_time * 10 seconds, floored at 10 s'\n")
    ok, detail = sweep_claims.check_card_silent_on_instance_ceiling(str(tmp_path))
    assert not ok and "shipped" in detail, detail


def test_the_detail_names_what_actually_matched(tmp_path):
    """The first version of this said "including the worked form ..." when only one of two markers
    had matched -- a sentence claiming more than the measurement, which is the habit this file
    exists to catch."""
    _make_task(tmp_path, "x = '(1 + 5) * reference_time * 10'\n")
    _, detail = sweep_claims.check_card_silent_on_instance_ceiling(str(tmp_path))
    assert "(1 + 5) * reference_time * 10" in detail
    assert "CEILING ON HOW SLOW" not in detail, detail


def test_item_a_would_hold_if_the_card_were_silent(tmp_path):
    _make_task(tmp_path, "x = 'nothing about ceilings here'\n")
    ok, detail = sweep_claims.check_card_silent_on_instance_ceiling(str(tmp_path))
    assert ok, detail


def test_item_b_still_holds_and_says_why_it_matters(tmp_path):
    """§84: eleven of seventeen multi-node probes ended on a node that was not their best, none on
    a better one, paired sign test p = 1/2048."""
    _make_task(tmp_path, "x = 'the champion is finally scored on held-out instances'\n")
    ok, detail = sweep_claims.check_card_silent_on_the_champion_rule(str(tmp_path))
    assert ok, detail
    assert "eleven of seventeen" in detail, detail


def test_item_b_would_flip_if_the_card_said_it(tmp_path):
    # THE MARKER AS SHIPPED, not a paraphrase: this fixture went red the moment the real clause
    # landed, which is the point -- a fixture written in words nobody uses tests nothing.
    _make_task(tmp_path, "x = 'BEST **EVALUATED** ONE, NOT YOUR LAST'\n")
    ok, detail = sweep_claims.check_card_silent_on_the_champion_rule(str(tmp_path))
    assert not ok and "NOT YOUR LAST" in detail, detail


def _drift(tmp_path: Path, rows):
    d = tmp_path / "looplab" / "benchmarks" / "algotune"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ruler_selfcheck_log.jsonl").write_text(
        "".join(json.dumps({"task": t, "median": m, "stamp": s, "subset": "test"}) + "\n"
                for t, m, s in rows), encoding="utf-8")


def test_every_constant_matching_its_measurement_holds(tmp_path):
    _drift(tmp_path, [(t, v, "2026-09-05T10:00:00") for t, v in sweep_claims.SWEEP_CONSTANTS.items()])
    ok, detail = sweep_claims.check_ruler_constants(str(tmp_path))
    assert ok, detail


def test_the_measured_disagreements_are_reported_with_their_direction(tmp_path):
    """Measured on this box: edge_expansion -7.8 %, discrete_log +7.2 %, pde_heat1d +10.6 %. Not a
    uniform box drift -- different tasks, different signs. Already recorded in §219; what was
    missing is anything that says so while the list keeps quoting the four numbers as current."""
    _drift(tmp_path, [("edge_expansion", 0.9077, "2026-09-05T12:39:01"),
                      ("pde_heat1d", 1.1013, "2026-09-04T15:37:39"),
                      ("discrete_log", 1.0896, "2026-09-04T15:39:43"),
                      ("pagerank", 1.0024, "2026-09-04T15:00:00")])
    ok, detail = sweep_claims.check_ruler_constants(str(tmp_path))
    assert not ok
    assert "-7.8 %" in detail and "+10.6 %" in detail and "+7.2 %" in detail, detail


def test_a_task_with_no_reading_is_unmeasured_not_passed(tmp_path):
    """Silence about a constant nobody has checked is how it stays quoted."""
    _drift(tmp_path, [("edge_expansion", 0.9847, "2026-09-05T10:00:00")])
    ok, detail = sweep_claims.check_ruler_constants(str(tmp_path))
    assert not ok and "pagerank: UNMEASURED" in detail, detail


def test_the_latest_reading_wins_not_the_first(tmp_path):
    """The log is append-only and holds two edge_expansion rows a day apart."""
    _drift(tmp_path, [(t, v, "2026-09-01T10:00:00") for t, v in sweep_claims.SWEEP_CONSTANTS.items()]
                     + [("edge_expansion", 0.9077, "2026-09-05T12:39:01")])
    ok, detail = sweep_claims.check_ruler_constants(str(tmp_path))
    assert not ok and "0.9077" in detail, detail


def test_an_unreadable_drift_log_is_not_a_pass(tmp_path):
    ok, detail = sweep_claims.check_ruler_constants(str(tmp_path))
    assert not ok and "cannot read" in detail, detail


def test_the_champion_marker_is_a_string_the_card_generator_actually_contains():
    """A checker matching on a phrase nobody wrote reports silence about a rule that is stated.

    That happened: the markers were guessed as "best evaluated" / "the best EVALUATED node" while
    the clause shipped as "BEST **EVALUATED** ONE, NOT YOUR LAST", so §271's own tool carried a
    false reading -- in the file built to catch false readings. This ties the marker to the source
    it is supposed to be looking for."""
    src = (Path(__file__).resolve().parents[1] / "benchmarks" / "algotune"
           / "make_task.py").read_text(encoding="utf-8", errors="replace")
    assert any(m in src for m in sweep_claims.CHAMPION_MARKS), sweep_claims.CHAMPION_MARKS


def test_the_ceiling_marker_is_too():
    src = (Path(__file__).resolve().parents[1] / "benchmarks" / "algotune"
           / "make_task.py").read_text(encoding="utf-8", errors="replace")
    assert any(m in src for m in sweep_claims.CEILING_MARKS), sweep_claims.CEILING_MARKS


def test_unmeasurable_and_merely_unmeasured_are_told_apart(tmp_path):
    """The self-check inlines the DELIVERED reference module, which exists only where a probe has
    staged one. Measured 2026-09-06: the only tasks with probe trees on this box are discrete_log,
    edge_expansion and pde_heat1d -- so `pagerank`'s constant is UNCHECKABLE here, not merely
    unchecked, and reporting the two the same way sends the reader to re-run a tool that will fail
    identically every sweep."""
    _drift(tmp_path, [("edge_expansion", 0.9847, "2026-09-05T10:00:00")])
    ws = tmp_path / "model-probes" / "p" / "ws" / "pde_heat1d"
    ws.mkdir(parents=True)
    (ws / "reference_pde_heat1d.py").write_text("class T(Task): pass\n", encoding="utf-8")
    _, detail = sweep_claims.check_ruler_constants(str(tmp_path))
    assert "pagerank: UNMEASURED here (no probe has staged its reference module" in detail, detail
    assert "pde_heat1d: UNMEASURED here (no reading recorded yet" in detail, detail


def _probe_with_score(root: Path, name: str, task: str, speedup, subset="test"):
    (root / "model-probes" / name / "runs" / task / "run").mkdir(parents=True, exist_ok=True)
    (root / "model-probes" / name / "runs" / task / "run" / "events.jsonl").write_text(
        "", encoding="utf-8")
    rec = {"subset": subset}
    if speedup is not None:
        rec["speedup"] = speedup
    (root / "model-probes" / name / "final.json").write_text(json.dumps(rec), encoding="utf-8")


def test_a_figure_still_in_the_corpus_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep_claims, "COMPARISON_FIGURES", {"discrete_log": [14.5186]})
    _probe_with_score(tmp_path, "dsDL", "discrete_log", 14.5186)
    ok, detail = sweep_claims.check_comparison_figures(str(tmp_path))
    assert ok and "still here (dsDL)" in detail, detail


def test_a_figure_whose_probe_is_gone_is_named(tmp_path, monkeypatch):
    """The figures are REAL and documented -- §68 and its tables -- and their probes are GONE: the
    2026-08-29 crash took /var/tmp with about 69 runs, `dsDL` and `dsDL2` among them. Point 9 reads
    as though they were the current corpus, and measured 2026-09-06 not one of the five is within
    0.005 of any probe now on this box."""
    monkeypatch.setattr(sweep_claims, "COMPARISON_FIGURES", {"discrete_log": [14.5186]})
    _probe_with_score(tmp_path, "remDL7", "discrete_log", 16.7799)
    ok, detail = sweep_claims.check_comparison_figures(str(tmp_path))
    assert not ok and "NOT among the 1 probe(s)" in detail, detail


def test_a_train_score_does_not_satisfy_a_test_figure(tmp_path, monkeypatch):
    """Every node runs on TRAIN and the champion is scored once on TEST (§84/§277); a train number
    that happens to match would be a different measurement wearing the right value."""
    monkeypatch.setattr(sweep_claims, "COMPARISON_FIGURES", {"discrete_log": [14.5186]})
    _probe_with_score(tmp_path, "dsDL", "discrete_log", 14.5186, subset="train")
    ok, detail = sweep_claims.check_comparison_figures(str(tmp_path))
    assert not ok, detail


def test_a_probe_on_another_task_does_not_satisfy_the_figure(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep_claims, "COMPARISON_FIGURES", {"discrete_log": [14.5186]})
    _probe_with_score(tmp_path, "elsewhere", "pagerank", 14.5186)
    ok, detail = sweep_claims.check_comparison_figures(str(tmp_path))
    assert not ok and "NOT among the 0 probe(s)" in detail, detail


def test_an_unreadable_final_does_not_take_the_check_down(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep_claims, "COMPARISON_FIGURES", {"discrete_log": [14.5186]})
    _probe_with_score(tmp_path, "dsDL", "discrete_log", 14.5186)
    (tmp_path / "model-probes" / "torn").mkdir(parents=True)
    (tmp_path / "model-probes" / "torn" / "final.json").write_text("{not json",
                                                                  encoding="utf-8")
    ok, detail = sweep_claims.check_comparison_figures(str(tmp_path))
    assert ok, detail


def test_the_campaign_evidence_claim_is_driven_not_grepped(tmp_path):
    """§267 closed this by DRIVING the real `archive_tree`, and the check re-drives it every sweep:
    a source-grep would pass on a function whose behaviour had changed underneath its comment.

    Given the real snapshot.sh, attempt 1 survives 400 rows as `.superseded-1` after `campaign.sh`'s
    rm -rf and an EQUAL-LENGTH attempt 2 -- equal length being the case a size test would miss and a
    prefix check catches."""
    bench = Path("/var/tmp/looplab-bench")
    if not (bench / "looplab" / "benchmarks" / "snapshot.sh").is_file():
        import pytest
        pytest.skip("no bench on this box")
    ok, detail = sweep_claims.check_campaign_evidence_overwrite(str(bench))
    assert not ok, detail
    assert "400 rows" in detail and "attempt1 row 0" in detail, detail
    assert "PREFIX check" in detail, detail


def test_a_bench_without_the_script_does_not_silently_pass(tmp_path):
    """"Cannot be driven" is not "the note is stale". Reporting a claim as refuted because the tool
    was missing is exactly the false reading this file exists to stop."""
    ok, detail = sweep_claims.check_campaign_evidence_overwrite(str(tmp_path))
    assert ok is False and "cannot be driven" in detail, detail


def test_a_bench_that_really_loses_the_evidence_reports_the_note_standing(tmp_path):
    """The check has to be able to say the note HOLDS, or it is a rubber stamp. A snapshot.sh whose
    archive_tree just copies -- no supersede -- must come back as the note standing."""
    d = tmp_path / "looplab" / "benchmarks"
    d.mkdir(parents=True)
    (d / "snapshot.sh").write_text(
        "archive_tree() {\n"
        '  mkdir -p "$2"\n'
        '  cp -ru "$1" "$2/"\n'
        "}\n", encoding="utf-8")
    ok, detail = sweep_claims.check_campaign_evidence_overwrite(str(tmp_path))
    assert ok is True and "NOT preserved" in detail, detail


def test_a_superseded_file_holding_the_wrong_attempt_is_not_intact(tmp_path):
    """The check has to look INSIDE. A version that accepted any `.superseded-1` passed every
    fixture here, because the one that loses evidence writes no such file at all -- so "exists" and
    "holds attempt 1" were never told apart. This archive_tree writes a superseded file containing
    attempt TWO, which is the failure that matters: evidence-shaped, evidence-free."""
    d = tmp_path / "looplab" / "benchmarks"
    d.mkdir(parents=True)
    (d / "snapshot.sh").write_text(
        "archive_tree() {\n"
        '  mkdir -p "$2/demo/run"\n'
        '  cp -r "$1"/. "$2/demo/" 2>/dev/null || true\n'
        '  cp "$1/run/events.jsonl" "$2/demo/run/events.jsonl.superseded-1"\n'
        "}\n", encoding="utf-8")
    ok, detail = sweep_claims.check_campaign_evidence_overwrite(str(tmp_path))
    assert ok is True, detail
    assert "does not hold attempt 1 intact" in detail, detail


def test_the_reason_a_constant_is_unmeasured_is_globbed_not_remembered(tmp_path):
    """The comment here used to name the three tasks with probe trees on the box, and pagerank was
    "UNCHECKABLE until a probe runs on that task" while `pgr1/ws/pagerank/` sat on disk, findable by
    the very glob one line above it -- with sixteen more under `_ruler/ws/`. Read quietly afterwards
    it gave 0.9994 against the quoted 1.0024.

    The fixture stages a module for one task and not another, so a check that remembered ANY list
    would have to be right about this box, which no fixture can arrange."""
    (tmp_path / "model-probes" / "someprobe" / "ws" / "pagerank").mkdir(parents=True)
    (tmp_path / "model-probes" / "someprobe" / "ws" / "pagerank"
     / "reference_pagerank.py").write_text("class T: pass\n", encoding="utf-8")
    (tmp_path / "looplab" / "benchmarks" / "algotune").mkdir(parents=True)
    (tmp_path / "looplab" / "benchmarks" / "algotune"
     / "ruler_selfcheck_log.jsonl").write_text("", encoding="utf-8")
    ok, detail = sweep_claims.check_ruler_constants(str(tmp_path))
    assert not ok
    # pagerank IS stageable here, so its reason must be "no reading", not "no module".
    assert "pagerank: UNMEASURED here (no reading recorded yet" in detail, detail
    # pde_heat1d is not staged, and the sentence carries the count the glob actually found.
    assert "pde_heat1d: UNMEASURED here (no probe has staged its reference module here (1 staged" \
        in detail, detail
