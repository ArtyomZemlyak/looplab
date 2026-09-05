"""The in-flight outlier check, and the mistake it made on its very first run.

Three sweeps in a row asked the same question by hand — is this evaluation time / phase share /
first-node point unusual? — and two of the three answers were "ordinary" while one was not, which is
never guessable without the distribution. `outlier_check.py` brings the distribution to the sweep.

Its first run flagged three healthy probes, because it compared a RUNNING probe's
`first_node_at / spend-so-far` against a corpus of FINAL shares: the same node reads 54 % at $0.59
and 32 % at its eventual $1.01. That is §209's mistake in different clothes — a partial quantity held
against a complete one — and it is what these tests pin.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import outlier_check  # noqa: E402


def _run(root: Path, name: str, costs, node_after=None, phase_costs=None):
    d = root / name / "runs" / "edge_expansion" / "run"
    d.mkdir(parents=True)
    events, spent = [], 0.0
    for c in costs:
        events.append({"type": "llm_usage", "data": {"cost": c}})
        spent += c
        if node_after is not None and spent >= node_after:
            events.append({"type": "node_evaluated", "data": {"node_id": 0, "metric": 200.0}})
            node_after = None
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    spans = [{"name": "generation", "attributes": {"phase": p, "cost": str(c)}}
             for p, c in (phase_costs or {"plan_step": 1.0}).items()]
    (d / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    return d


def test_the_first_node_is_measured_in_dollars_not_in_a_share_of_partial_spend(tmp_path):
    """The same node, read twice: once when the run has spent $0.60 and once at $1.00. A share moves;
    dollars do not."""
    _run(tmp_path, "midway", [0.1] * 6, node_after=0.3)
    _run(tmp_path, "done", [0.1] * 10, node_after=0.3)
    mid = outlier_check.measure(str(tmp_path / "midway" / "runs" / "edge_expansion" / "run" /
                                    "events.jsonl"), "nope")
    done = outlier_check.measure(str(tmp_path / "done" / "runs" / "edge_expansion" / "run" /
                                     "events.jsonl"), "nope")
    assert abs(mid["first_node_usd"] - done["first_node_usd"]) < 1e-9, (
        f'{mid["first_node_usd"]} vs {done["first_node_usd"]}: the first node moved because the run '
        "kept spending, which is exactly the bias this replaced")
    assert "first_node_pct" not in mid, "the share is back, and it is not comparable mid-run"


def test_spend_and_node_count_are_not_compared_for_a_running_probe(tmp_path, monkeypatch, capsys):
    """A running probe has spent less and built fewer nodes than any finished one, so comparing them
    flags every live probe every time -- an alarm that means nothing trains its reader to skip the
    ones that do."""
    for i in range(6):
        _run(tmp_path, f"done{i}", [0.1] * 10, node_after=0.3)
    _run(tmp_path, "live", [0.1] * 4, node_after=0.3)
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda *a, **k: [{"probe": "live", "pid": 1, "cpus": {0}}])
    assert outlier_check.main(["--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "spend=" not in out and "nodes=" not in out, out
    assert "nothing outside" in out, out


def test_a_genuine_outlier_is_still_named(tmp_path, monkeypatch, capsys):
    """capA9's shape: a first node at $0.64 where the corpus median is $0.32."""
    for i in range(9):
        _run(tmp_path, f"done{i}", [0.1] * 10, node_after=0.3)
    _run(tmp_path, "late", [0.1] * 8, node_after=0.7)
    monkeypatch.setattr(outlier_check.lanes, "probes",
                        lambda *a, **k: [{"probe": "late", "pid": 1, "cpus": {0}}])
    outlier_check.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "first_node_usd" in out and "OUTSIDE" in out, out


def test_it_reads_no_score(tmp_path):
    """§190 forbids reading the arm's outcome in flight, and a hygiene tool that grew a metric
    column would be an interim read with a friendly name."""
    src = (Path(__file__).resolve().parents[1] / "benchmarks" / "outlier_check.py").read_text(
        encoding="utf-8")
    body = src.split('"""', 2)[2]
    for forbidden in ("final.json", "speedup", "champion"):
        assert forbidden not in body, f"outlier_check reaches for {forbidden!r}"
    got = outlier_check.measure(
        str(_run(tmp_path, "p", [0.1] * 5, node_after=0.2) / "events.jsonl"), "nope")
    assert "metric" not in got and set(got) >= {"spend", "nodes", "first_node_usd"}


def test_a_value_equal_to_many_others_is_not_extreme():
    """Ties are not evidence of extremity. Counting `<= value` puts a probe sitting exactly on a
    much-repeated value at the 100th percentile and flags it for being typical; the midrank
    convention puts it in the middle. Mutation found this: after the constant-corpus guard was
    fixed, no fixture exercised a tie inside a corpus that varies."""
    sample = [0.1, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.9]
    assert 40 <= outlier_check.percentile(sample, 0.3) <= 60, (
        f"{outlier_check.percentile(sample, 0.3)}: a value shared with seven of nine runs is not "
        "an outlier")
    assert outlier_check.percentile(sample, 0.9) > 90
    assert outlier_check.percentile(sample, 0.1) < 10
