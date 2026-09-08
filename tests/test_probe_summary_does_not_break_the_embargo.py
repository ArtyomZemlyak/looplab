"""The tool that answers checklist item 9 prints a TEST column for every probe it finds.

The arm's probes live in the same tree as everything else, so pointing it at `BENCH_ROOT` -- its
documented default -- puts the arm's outcome on screen, which is the one thing §190 forbids and
which `arm_fidelity`, `pulse`, `outlier_check` and `lane_balance` were each built to avoid. I came
within one command of it while trying to check this very tool against a NON-arm probe. Same shape as
§270: the tools obeyed and the operator did not.

And the argument that got me there: `probe_summary.py accEE` -- a probe NAME, which is what the
checklist talks in -- was filtered out silently and the tool reported "no bench roots on this box",
a message about the BOX for a mistake on the command line. With two arguments it is worse: a
mistyped root is dropped beside a good one and the report quietly covers a different scope.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import probe_summary  # noqa: E402


def _design(tmp_path: Path, batches) -> str:
    body = ",\n    ".join(f"({t!r}, {c!r})" for t, c in batches)
    path = tmp_path / "arm_readout.py"
    path.write_text(f"BATCHES = [\n    {body},\n]\n", encoding="utf-8")
    return str(path)


def test_an_argument_that_is_not_a_root_is_named(tmp_path, capsys):
    assert probe_summary._roots(["accEE"]) == []
    err = capsys.readouterr().err
    assert "not a directory: accEE" in err, err
    assert "probe names" in err, err


def test_a_good_root_beside_a_bad_one_does_not_silently_narrow_the_scope(tmp_path, capsys):
    """The dangerous version: one valid root and one typo, a report that looks like it worked and
    covers half of what was asked for."""
    (tmp_path / "real").mkdir()
    assert probe_summary._roots([str(tmp_path / "real"), "/no/such/root"]) == []
    assert "not a directory: /no/such/root" in capsys.readouterr().err


def test_every_probe_in_the_registered_design_is_embargoed(tmp_path, monkeypatch):
    design = _design(tmp_path, [(["capA1", "capB1"], ["freeA1", "freeB1"])])
    monkeypatch.setattr(probe_summary, "EMBARGO_LIFTED", tmp_path / "not-there")
    got = probe_summary.embargoed_probes(design)
    assert got == {"capA1", "capB1", "freeA1", "freeB1"}, got


def test_the_embargo_lifts_only_when_the_readout_is_marked_taken(tmp_path, monkeypatch):
    """A file, not an inference. Lifting §190 should be a deliberate act somebody can find in the
    history, not a side effect of some other state happening to look complete."""
    design = _design(tmp_path, [(["capA1", "capB1"], ["freeA1", "freeB1"])])
    marker = tmp_path / "taken"
    monkeypatch.setattr(probe_summary, "EMBARGO_LIFTED", marker)
    assert probe_summary.embargoed_probes(design)
    marker.write_text("read 2026-09-05\n", encoding="utf-8")
    assert probe_summary.embargoed_probes(design) == set()


def test_a_design_that_cannot_be_read_is_not_a_licence_to_print(tmp_path, monkeypatch):
    """If the membership list is missing or malformed, the safe answer is "I do not know who is in
    the arm", and printing every score is not that answer... but neither is masking nothing. The
    conservative direction here is the one that cannot leak: an unreadable design yields an empty
    set only because there is then no arm on this box at all -- so the test pins that the call does
    not RAISE, and the caller's own default stays masked when a design does exist."""
    assert probe_summary.embargoed_probes(str(tmp_path / "nope.py")) == set()
    design = _design(tmp_path, [(["capA1", "capB1"], ["freeA1", "freeB1"])])
    monkeypatch.setattr(probe_summary, "EMBARGO_LIFTED", tmp_path / "not-there")
    assert "capA1" in probe_summary.embargoed_probes(design)


def _probe(root: Path, name: str, speedup=224.8846):
    run = root / name / "runs" / "edge_expansion" / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(
        json.dumps({"type": "llm_usage", "data": {"cost": 1.0}}) + "\n"
        + json.dumps({"type": "node_evaluated",
                      "data": {"node_id": 0, "metric": 200.0, "eval_seconds": 44.0}}) + "\n",
        encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    (root / name / "final.json").write_text(json.dumps({"speedup": speedup, "subset": "test"}),
                                            encoding="utf-8")


def _report(tmp_path, monkeypatch, capsys, argv, lifted=False):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    _probe(root, "capA1")
    _probe(root, "accEE", speedup=111.0)
    monkeypatch.setattr(probe_summary, "ARM_DESIGN",
                        _design(tmp_path, [(["capA1", "capB1"], ["freeA1", "freeB1"])]))
    marker = tmp_path / "taken"
    if lifted:
        marker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(probe_summary, "EMBARGO_LIFTED", marker)
    probe_summary.main([str(root), *argv])
    return capsys.readouterr().out


def test_an_arm_probes_score_never_reaches_the_table(tmp_path, monkeypatch, capsys):
    out = _report(tmp_path, monkeypatch, capsys, [])
    assert "EMBARGO" in out and "224.8846" not in out, out
    assert "111.0000" in out, f"a probe OUTSIDE the arm lost its score too: {out}"


def test_the_masking_says_it_is_masking(tmp_path, monkeypatch, capsys):
    """A blank or a dash would hide the arm and also hide that anything was hidden."""
    out = _report(tmp_path, monkeypatch, capsys, [])
    assert "1 probe(s) in the registered arm" in out and "§190" in out, out


def test_the_override_is_explicit_and_works(tmp_path, monkeypatch, capsys):
    out = _report(tmp_path, monkeypatch, capsys, ["--include-embargoed"])
    assert "224.8846" in out and "EMBARGO" not in out, out


def test_marking_the_readout_taken_lifts_it(tmp_path, monkeypatch, capsys):
    out = _report(tmp_path, monkeypatch, capsys, [], lifted=True)
    assert "224.8846" in out and "EMBARGO" not in out, out


def test_probe_filters_to_one_name(tmp_path, monkeypatch, capsys):
    out = _report(tmp_path, monkeypatch, capsys, ["--probe", "accEE"])
    assert "accEE" in out and "capA1" not in out, out


def test_json_carries_the_numbers_item_nine_keeps_grepping_for(tmp_path, monkeypatch, capsys):
    """Checklist item 9 kept being answered by grepping this tool's prose, and on 2026-09-06 that
    produced a FALSE ARM DIFFERENCE. The reference line reads

        reference over 12 executed run_probe calls (+10 refused at the cap): 8.3% import

    for a capped probe and `... run_probe calls: 5.0% import` for an uncapped one, so a regex
    expecting `calls:` dropped every treated probe and reported "treat 0.0 %, control 9.1 %" off 24
    of 48 rows. The parenthesis correlates perfectly with the arm. From the data the two are 8.3 %
    and 9.0 %."""
    out = _report(tmp_path, monkeypatch, capsys, ["--json"], lifted=True)
    rows = json.loads(out)
    assert rows and all("probe" in r for r in rows)
    for key in ("ref_pct", "ref_call_pct", "run_probe", "run_probe_refused", "eval_train"):
        assert key in rows[0], (key, sorted(rows[0]))


def test_json_still_honours_the_embargo(tmp_path, monkeypatch, capsys):
    """A machine-readable escape hatch around §190 would be worse than the prose one, not better."""
    rows = json.loads(_report(tmp_path, monkeypatch, capsys, ["--json"]))
    by = {r["probe"]: r for r in rows}
    assert by["capA1"]["test"] is None and by["capA1"]["embargoed"] is True, by["capA1"]
    assert by["accEE"]["test"] == 111.0 and "embargoed" not in by["accEE"], by["accEE"]


def test_json_respects_the_probe_filter(tmp_path, monkeypatch, capsys):
    rows = json.loads(_report(tmp_path, monkeypatch, capsys, ["--json", "--probe", "accEE"]))
    assert [r["probe"] for r in rows] == ["accEE"], rows


def test_json_is_parseable_not_merely_printed(tmp_path, monkeypatch, capsys):
    """`default=str` is there so one unserialisable field cannot take the whole report down."""
    out = _report(tmp_path, monkeypatch, capsys, ["--json"], lifted=True)
    assert json.loads(out) == json.loads(out)


def test_the_reference_band_gains_the_task_s_own_middle(tmp_path, monkeypatch, capsys):
    """§69.1's 4.9-8.3 % is one number for every task, and the tasks differ. Measured 2026-09-06
    over the 140 probes with five or more executed run_probe calls: median import share 8.6 % on
    edge_expansion, 4.8 % on discrete_log, 8.3 % on pde_heat1d, permutation p = 0.008 that the task
    means are the same. The band covers 19 %, 27 % and 45 % of those tasks respectively -- it fits
    none of them, and it was never measured per task."""
    root = tmp_path / "root"
    root.mkdir()
    for i in range(6):
        _probe(root, f"ee{i}", speedup=100.0 + i)
    monkeypatch.setattr(probe_summary, "ARM_DESIGN", _design(tmp_path, []))
    monkeypatch.setattr(probe_summary, "EMBARGO_LIFTED", tmp_path / "taken")
    (tmp_path / "taken").write_text("x", encoding="utf-8")
    probe_summary.main([str(root)])
    out = capsys.readouterr().out
    assert "§69.1 baseline 4.9-8.3 %" in out, out
    assert ("this task:" in out) or ("too few for a middle" in out), out


def test_a_filtered_run_says_why_it_has_no_middle(tmp_path, monkeypatch, capsys):
    """`--probe` skips summarising everything else, so a middle computed then would be over the one
    probe asked for -- and omitting it silently looks exactly like a task that genuinely has too few
    probes. Two different absences, two sentences."""
    out = _report(tmp_path, monkeypatch, capsys, ["--probe", "accEE"], lifted=True)
    assert "this task's middle needs the unfiltered run" in out, out
    assert "too few for a middle" not in out, out


def test_a_thin_task_says_it_is_thin_rather_than_going_quiet(tmp_path, monkeypatch, capsys):
    out = _report(tmp_path, monkeypatch, capsys, [], lifted=True)
    assert "too few for a middle" in out, out


def _probing_probe(root: Path, name: str, calls: int, ref_imports: int, speedup=100.0):
    """A probe that really ran `run_probe`, so `ref_pct` has a denominator."""
    run = root / name / "runs" / "edge_expansion" / "run"
    run.mkdir(parents=True)
    rows = []
    for i in range(calls):
        body = "import reference_edge_expansion" if i < ref_imports else "print(1)"
        rows.append({"kind": "tool", "name": "tool",
                     "attributes": {"tool": "run_probe", "phase_span": f"s{i}",
                                    "code": body, "output": "exit=0"}})
    (run / "spans.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (run / "events.jsonl").write_text(
        json.dumps({"type": "llm_usage", "data": {"cost": 1.0}}) + "\n", encoding="utf-8")
    (root / name / "final.json").write_text(json.dumps({"speedup": speedup, "subset": "test"}),
                                            encoding="utf-8")


def test_a_share_quantized_to_quarters_stays_out_of_the_task_middle(tmp_path, monkeypatch, capsys):
    """The threshold is five EXECUTED calls, and the reason is resolution, not zero.

    A probe with no calls at all has `ref_pct` of `None` and never reaches the median anyway -- so a
    fixture built from those agreed with the mutation and let it survive twice. The case the
    threshold actually excludes is a probe with ONE to FOUR calls, whose share can only be 0, 25, 50,
    75 or 100 %: a number, admitted by any `isinstance` check, and pure quantization noise in a
    median. Six such probes against five real ones move the middle from 10.0 % to 0.0 %."""
    root = tmp_path / "root"
    root.mkdir()
    for i in range(5):
        _probing_probe(root, f"real{i}", calls=10, ref_imports=1, speedup=100.0 + i)
    # SIX idle probes against five real ones, and not four: with four the mutation that lets them
    # in leaves the median at 10.0 anyway (0,0,0,0,10,10,10,10,10 -> the middle is still 10), so the
    # fixture agreed with the bug. Six moves the middle to 0.0 and the count from 5 to 11.
    for i in range(6):
        _probing_probe(root, f"coarse{i}", calls=2, ref_imports=0, speedup=50.0 + i)
    monkeypatch.setattr(probe_summary, "ARM_DESIGN", _design(tmp_path, []))
    monkeypatch.setattr(probe_summary, "EMBARGO_LIFTED", tmp_path / "taken")
    (tmp_path / "taken").write_text("x", encoding="utf-8")
    probe_summary.main([str(root)])
    out = capsys.readouterr().out
    assert "this task: 5 probes, median 10.0 %" in out, out


def test_the_middle_is_the_median_of_probes_that_actually_probed(tmp_path, monkeypatch):
    """A probe with no executed `run_probe` calls has a reference share of 0 % by construction, and
    letting those into the median would drag every task's middle toward zero."""
    task_ref = {"t": [4.0, 6.0, 8.0, 10.0, 12.0]}
    assert "median 8.0 %" in probe_summary._task_ref_note(task_ref, "t")
    assert probe_summary._task_ref_note({"t": [1.0]}, "t").endswith("too few for a middle")
