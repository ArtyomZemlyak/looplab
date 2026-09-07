"""A pair is only "matched on budget" if the METER says so.

Measured on 2026-08-26, mid-campaign: arm A's `rbf_interpolation` drew $2.009 against a $1.00
ceiling. Seventeen consecutive streams of ~220k tokens each ran ~1808 s and were ended upstream
with no `usage` frame; AlgoTuner prices from that frame, so its ledger recorded them as free and
its `spend_limit` fired at twice the real spend -- "Spend limit of $1.0000 reached. Current spend:
$1.0025", in its own log, while the gateway had charged $2.009. Arm B prices from the proxy's
ledger and all twenty of its task-arms landed at or under $1.011.

The comparison table printed those two arms side by side and said nothing about it. These tests
fail if it goes back to saying nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "compare_arms.py"


def _meter_row(arm: str, task: str, cost: float, aborted: bool = False,
               attempt: str = "a3") -> str:
    row = {"ts": 1787716140.0, "arm": arm, "task": task, "attempt": attempt, "status": 200,
           "cost": cost, "cost_basis": "imputed"}
    if aborted:
        row.update({"stream_aborted": True, "cost_basis": "estimated_from_deltas"})
    return json.dumps(row)


def _campaign(tmp_path: Path, *, a_spend: float, a_aborted: float = 0.0) -> Path:
    """A one-task campaign with both arms scored, so the row itself is never the reason to complain."""
    root = tmp_path / "bench"
    algotune = root / "AlgoTune"
    (algotune / "reports").mkdir(parents=True)
    (algotune / "reports" / "agent_summary.json").write_text(
        json.dumps({"rbf_interpolation": {"gateway/deepseek-v4-flash": {"final_speedup": 1.0579}}}),
        encoding="utf-8")

    runs = root / "runs-B" / "rbf_interpolation" / "run"
    runs.mkdir(parents=True)
    (runs / "events.jsonl").write_text("", encoding="utf-8")

    final = root / "campaign-final"
    final.mkdir(parents=True)
    (final / "B-rbf_interpolation.final.json").write_text(
        json.dumps({"speedup": 1.0466, "subset": "test"}), encoding="utf-8")
    (final / "B-rbf_interpolation.done").write_text(
        "wall=100 rc=0 state=ran_to_completion ok_calls=5 attempt=a1\n", encoding="utf-8")
    (final / "A-rbf_interpolation.done").write_text(
        "wall=100 rc=0 state=ran_to_completion ok_calls=5 attempt=a3\n", encoding="utf-8")

    meter = root / "meter"
    meter.mkdir(parents=True)
    rows = [_meter_row("B", "rbf_interpolation", 1.003, attempt="a1")]
    if a_aborted:
        rows.append(_meter_row("A", "rbf_interpolation", a_aborted, aborted=True))
    rows.append(_meter_row("A", "rbf_interpolation", a_spend - a_aborted))
    (meter / "meter.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return root


def _run(root: Path) -> str:
    out = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--algotune-root", str(root / "AlgoTune"),
         "--runs-root", str(root / "runs-B"),
         "--final-dir", str(root / "campaign-final"),
         "--budget-usd", "1.00"],
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_an_arm_that_drew_double_its_ceiling_is_named_under_the_table():
    """The exact shape measured on 2026-08-26. Silence here is the defect this test exists for."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = _campaign(Path(td), a_spend=2.009, a_aborted=1.006)
        out = _run(root)
    assert "2.009" in out, f"the real spend never reached the table:\n{out}"
    assert "NOT matched on budget" in out, f"the pair was presented as fair:\n{out}"
    assert "arm A" in out
    # And WHY it was invisible to that loop, or the reader cannot tell this from overspending.
    assert "no usage frame" in out, f"the cause was dropped:\n{out}"


def test_a_pair_that_stayed_inside_its_ceiling_is_said_to_have_done_so():
    """The falsifier for a check that just always shouts: within budget must read as within budget."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = _campaign(Path(td), a_spend=1.011)
        out = _run(root)
    assert "within 5%" in out, f"a clean pair was not certified clean:\n{out}"
    assert "NOT matched on budget" not in out, f"a clean pair was flagged:\n{out}"


def test_the_totals_come_from_the_meter_not_from_either_loop():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = _campaign(Path(td), a_spend=2.009, a_aborted=1.006)
        out = _run(root)
    assert "arm A $2.01" in out and "arm B $1.00" in out, out


def test_no_ledger_means_no_claim_rather_than_a_wrong_one():
    """An absent meter must not turn into "everything was fine"."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = _campaign(Path(td), a_spend=2.009, a_aborted=1.006)
        (root / "meter" / "meter.jsonl").unlink()
        out = _run(root)
    assert "metered spend" not in out, f"claimed a spend it could not read:\n{out}"
    assert "within 5%" not in out, f"certified a budget it never saw:\n{out}"


def test_an_abandoned_attempt_is_not_charged_to_the_reported_one():
    """The first version of this check summed a task across attempts and was wrong on real data.

    Two gateway outages on 2026-08-25 forced whole task-arms to be re-run; the abandoned attempts
    are still in the ledger with their scores already discarded. Summing them turned `convex_hull`
    -- two attempts, neither above $1.02 -- into a $2.077 "overspend", and the check named nine
    offenders where a per-attempt count finds five. The marker says which attempt was scored; only
    that attempt's money is the price of the printed number.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = _campaign(Path(td), a_spend=1.010)
        meter = root / "meter" / "meter.jsonl"
        # An earlier attempt of the SAME task, killed by an outage, its score thrown away.
        meter.write_text(meter.read_text(encoding="utf-8")
                         + _meter_row("A", "rbf_interpolation", 1.067, attempt="a2") + "\n",
                         encoding="utf-8")
        out = _run(root)
    assert "NOT matched on budget" not in out, (
        "the abandoned attempt was charged to the scored one:\n" + out)
    assert "within 5%" in out, out
