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
