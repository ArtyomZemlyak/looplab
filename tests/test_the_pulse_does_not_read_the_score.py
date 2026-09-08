"""The sweep's own point 2 was an interim read, every half hour, for nine batches.

`arm_fidelity` was built so fidelity could be asked continuously without looking at the outcome, and
`test_arm_fidelity_reads_no_scores.py` holds it to that. Meanwhile point 2 -- "new nodes, zeros and
errors" -- was answered by a heredoc that printed the node METRICS. §190 forbids reading the arm's
outcome before twelve batches. The tool obeyed; the operator did not.

Point 2 never needed the value: it needs whether nodes arrived, whether any were zero, and whether a
zero is the harness declining (`eval_seconds` under five) or an evaluation that ran and came back
invalid (all 12 corpus zeros, at 41-47 s). These tests pin the negative property BEHAVIOURALLY -- a
distinctive score must not reach the output -- rather than by scanning for a token, because the
classification legitimately has to read the field it must never print.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import pulse  # noqa: E402

LOUD = 123456.789        # a score no other number in the output could be confused with


def _probe(root: Path, name: str, *, scored=(), zeros=(), spend=0.5, errors=0):
    d = root / name / "runs" / "edge_expansion" / "run"
    d.mkdir(parents=True)
    rows = [{"type": "llm_usage", "data": {"cost": spend}}]
    for i, metric in enumerate(scored):
        rows.append({"type": "node_evaluated", "data": {"node_id": i, "metric": metric,
                                                        "eval_seconds": 44.0}})
    for i, secs in enumerate(zeros, start=len(scored)):
        rows.append({"type": "node_evaluated",
                     "data": {"node_id": i, "metric": 0.0, "eval_seconds": secs,
                              "violations": 3}})
    for _ in range(errors):
        rows.append({"type": "error", "data": {}})
    (d / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return d


def _run_main(tmp_path, monkeypatch, capsys, name="live", cpus=frozenset(range(11)), **kw):
    monkeypatch.setattr(pulse.lanes, "probes",
                        lambda bench: [{"pid": 1, "probe": name, "cpus": set(cpus), "argv": []}])
    rc = pulse.main(["--bench", str(tmp_path), "--root", str(tmp_path), *kw.pop("argv", [])])
    return rc, capsys.readouterr().out


def test_a_score_never_reaches_the_output(tmp_path, monkeypatch, capsys):
    _probe(tmp_path, "live", scored=(LOUD, 2 * LOUD))
    _, out = _run_main(tmp_path, monkeypatch, capsys)
    assert "123456" not in out, out
    assert "246913" not in out, out
    assert " 2 " in out or "2\n" in out or "    2" in out, f"the node COUNT vanished too: {out!r}"


def test_the_count_is_still_reported(tmp_path, monkeypatch, capsys):
    """Suppressing the value by suppressing the whole line would pass the test above and destroy
    the point of the tool."""
    got = pulse.pulse(str(_probe(tmp_path, "live", scored=(LOUD, LOUD, LOUD)) / "events.jsonl"))
    assert got["nodes"] == 3 and got["zeros"] == 0, got


def test_a_zero_under_five_seconds_is_named_a_ruler_refusal(tmp_path, monkeypatch, capsys):
    _probe(tmp_path, "live", scored=(LOUD,), zeros=(0.1,))
    _, out = _run_main(tmp_path, monkeypatch, capsys)
    assert "RULER REFUSAL" in out, out
    assert "eval_seconds=0.1" in out, out


def test_a_zero_at_forty_five_seconds_is_not(tmp_path, monkeypatch, capsys):
    """All 12 zeros in the corpus are this kind, at 41-47 s, carrying `violations`. Calling them
    refusals would send the next hour after the harness instead of the solver."""
    _probe(tmp_path, "live", scored=(LOUD,), zeros=(45.7,))
    _, out = _run_main(tmp_path, monkeypatch, capsys)
    assert "RULER REFUSAL" not in out, out
    assert "came back invalid" in out and "violations=3" in out, out


def test_a_stalled_log_is_an_exit_code_not_just_a_line(tmp_path, monkeypatch, capsys):
    _probe(tmp_path, "live", scored=(LOUD,))
    monkeypatch.setattr(pulse.lanes, "probes",
                        lambda bench: [{"pid": 1, "probe": "live", "cpus": set(range(11)),
                                        "argv": []}])
    import os
    p = tmp_path / "live" / "runs" / "edge_expansion" / "run" / "events.jsonl"
    rc = pulse.main(["--bench", str(tmp_path), "--root", str(tmp_path), "--stall", "2400",
                     "--now", str(os.path.getmtime(p) + 2401)])
    out = capsys.readouterr().out
    assert rc == 1 and "STALLED" in out, (rc, out)


def test_a_fresh_log_is_not_stalled(tmp_path, monkeypatch, capsys):
    _probe(tmp_path, "live", scored=(LOUD,))
    rc, out = _run_main(tmp_path, monkeypatch, capsys)
    assert rc == 0 and "STALLED" not in out, (rc, out)


def _events(root: Path, name: str, rows):
    d = root / name / "runs" / "edge_expansion" / "run"
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return d


def _absent(tmp_path, monkeypatch, capsys, expect, running=()):
    monkeypatch.setattr(pulse.lanes, "probes",
                        lambda bench: [{"pid": 1, "probe": n, "cpus": set(range(11)), "argv": []}
                                       for n in running])
    rc = pulse.main(["--bench", str(tmp_path), "--root", str(tmp_path), "--expect", *expect])
    return rc, capsys.readouterr().out


def test_a_probe_that_ended_is_told_apart_from_one_that_stopped(tmp_path, monkeypatch, capsys):
    """`pulse` lists what is RUNNING, so a probe killed by a signal simply stops appearing and reads
    exactly like one that finished. The only reason that was ever caught is `check_money`, which
    found FIVE of them as money in the meter with no tree on disk. The liveness tool should not need
    the money tool to notice a missing probe."""
    _events(tmp_path, "ended", [{"type": "llm_usage", "data": {"cost": 1.01}},
                                {"type": "run_finished", "data": {"reason": "budget_exhausted"}}])
    _events(tmp_path, "stopped", [{"type": "llm_usage", "data": {"cost": 0.42}}])
    rc, out = _absent(tmp_path, monkeypatch, capsys, ["ended", "stopped"])
    assert "ended" in out and "VANISHED" in out, out
    assert "it did not finish, it stopped" in out, out
    assert rc == 1, out


def test_a_probe_at_its_ceiling_ended_even_though_it_says_pause(tmp_path, monkeypatch, capsys):
    """§228: 16 corpus runs reached full budget and were PAUSED by a blanket handler dressing the
    ceiling refusal as a provider failure. Those runs are complete; calling them owed work is what
    sent freeB3 back for another $0.1056 (§213)."""
    _events(tmp_path, "atceiling", [{"type": "llm_usage", "data": {"cost": 1.004}},
                                    {"type": "pause", "data": {}}])
    rc, out = _absent(tmp_path, monkeypatch, capsys, ["atceiling"])
    assert "ended" in out and "VANISHED" not in out and "OWED" not in out, out
    assert rc == 0, out


def test_a_genuine_pause_below_the_ceiling_is_owed_work(tmp_path, monkeypatch, capsys):
    _events(tmp_path, "owed", [{"type": "llm_usage", "data": {"cost": 0.40}},
                               {"type": "pause", "data": {}}])
    rc, out = _absent(tmp_path, monkeypatch, capsys, ["owed"])
    assert "PAUSED and owed work" in out and rc == 1, (rc, out)


def test_a_probe_still_on_its_lane_is_not_called_absent(tmp_path, monkeypatch, capsys):
    _probe(tmp_path, "live", scored=(LOUD,))
    rc, out = _absent(tmp_path, monkeypatch, capsys, ["live"], running=["live"])
    assert "off the lanes" not in out and rc == 0, (rc, out)


def _ledger(tmp_path: Path, rows):
    d = tmp_path / "meter"
    d.mkdir(parents=True, exist_ok=True)
    (d / "meter.jsonl").write_text(
        "".join(json.dumps({"ts": ts, "arm": arm, "status": st}) + "\n" for arm, ts, st in rows),
        encoding="utf-8")
    return str(d / "meter.jsonl")


def _with_ledger(tmp_path, monkeypatch, capsys, rows, log_age, now=1_000_000.0, stall=2400.0):
    import os as _os
    _probe(tmp_path, "live", scored=(LOUD,))
    p = tmp_path / "live" / "runs" / "edge_expansion" / "run" / "events.jsonl"
    _os.utime(p, (now - log_age, now - log_age))
    monkeypatch.setattr(pulse.lanes, "probes",
                        lambda bench: [{"pid": 1, "probe": "live", "cpus": set(range(11)),
                                        "argv": []}])
    rc = pulse.main(["--bench", str(tmp_path), "--root", str(tmp_path),
                     "--ledger", _ledger(tmp_path, rows), "--now", str(now),
                     "--stall", str(stall)])
    return rc, capsys.readouterr().out


def test_the_second_clock_point_four_asks_for_is_shown(tmp_path, monkeypatch, capsys):
    """Point 4 asks for the age of events.jsonl AND the age of the last CALL in the ledger. The tool
    was showing one of them."""
    _, out = _with_ledger(tmp_path, monkeypatch, capsys, [("live", 999_940.0, "200")], log_age=30)
    assert "call age" in out and "60s" in out, out


def test_a_fresh_ledger_beside_a_stale_log_is_named(tmp_path, monkeypatch, capsys):
    """The retry-storm shape: the probe is calling and producing nothing. §175 is the record, and
    three consecutive 504s at exactly 300 s are the nginx ceiling rather than a hang."""
    _, out = _with_ledger(tmp_path, monkeypatch, capsys, [("live", 999_990.0, "200")],
                          log_age=1200)
    assert "CALLING BUT NOT PRODUCING" in out, out


def test_a_stale_ledger_beside_a_fresh_log_is_ordinary(tmp_path, monkeypatch, capsys):
    """Measured on the live batch: capB13's last call was 316 s old while its log had grown 20 s
    ago -- a probe building or evaluating without calling the model. Flagging that would fire on
    every healthy probe mid-evaluation."""
    _, out = _with_ledger(tmp_path, monkeypatch, capsys, [("live", 999_684.0, "200")], log_age=20)
    assert "CALLING BUT NOT PRODUCING" not in out, out
    assert "316s" in out, out


def test_a_non_200_last_call_is_named(tmp_path, monkeypatch, capsys):
    _, out = _with_ledger(tmp_path, monkeypatch, capsys, [("live", 999_990.0, "401")], log_age=30)
    assert "came back 401" in out, out


def test_a_probe_with_no_ledger_row_still_reports(tmp_path, monkeypatch, capsys):
    """A probe that has not made its first call yet has no row, and a dash is the honest reading --
    not a zero, and not a crash."""
    rc, out = _with_ledger(tmp_path, monkeypatch, capsys, [("someone-else", 999_990.0, "200")],
                           log_age=30)
    assert rc == 0 and "live" in out, out
    assert "CALLING BUT NOT PRODUCING" not in out, out
