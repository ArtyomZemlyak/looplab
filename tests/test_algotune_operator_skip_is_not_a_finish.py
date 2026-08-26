"""A task-arm the operator skipped must not be counted among the finished ones.

`campaign.sh::already_measured` skips on ANY non-empty marker that is not a wall cut. That is how a
running campaign is told to stop taking new work without editing the driver a live bash is reading
incrementally — and it is the right mechanism. What it leaves behind looks, to every later reader,
exactly like a completed run.

Used for real on 2026-08-26: five CP-SAT task-arms were skipped by decision so the campaign could
be closed after the batch in flight. The pairs are already excluded from the means, because
`agent_summary.json` carries no score for a task that never ran — what these tests pin is that the
table says the WORD, and says how many of its rows are missing an arm on purpose.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "compare_arms.py"

DONE = "wall=100 rc=0 state=ran_to_completion ok_calls=5 attempt=a1\n"
SKIP = "wall=0 rc=0 state=operator_skip attempt=a1\n"


def _campaign(tmp: Path, *, a_marker: str, a_score: bool) -> Path:
    root = tmp / "bench"
    (root / "AlgoTune" / "reports").mkdir(parents=True)
    summary = ({"demo": {"gateway/deepseek-v4-flash": {"final_speedup": 2.5}}} if a_score else {})
    (root / "AlgoTune" / "reports" / "agent_summary.json").write_text(json.dumps(summary),
                                                                     encoding="utf-8")
    (root / "runs-B" / "demo" / "run").mkdir(parents=True)
    (root / "runs-B" / "demo" / "run" / "events.jsonl").write_text("", encoding="utf-8")
    final = root / "campaign-final"
    final.mkdir(parents=True)
    (final / "B-demo.final.json").write_text(json.dumps({"speedup": 1.5, "subset": "test"}),
                                             encoding="utf-8")
    (final / "B-demo.done").write_text(DONE, encoding="utf-8")
    (final / "A-demo.done").write_text(a_marker, encoding="utf-8")
    return root


def _run(root: Path) -> str:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--algotune-root", str(root / "AlgoTune"),
         "--runs-root", str(root / "runs-B"), "--final-dir", str(root / "campaign-final")],
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_a_skipped_arm_is_named_as_skipped(tmp_path):
    out = _run(_campaign(tmp_path, a_marker=SKIP, a_score=False))
    assert "SKIPPED by the operator and never ran" in out, out
    assert "demo" in out
    assert "over 0 of 1 tasks" in out, "it does not say how much of the table is missing"


def test_a_skipped_arm_is_not_averaged(tmp_path):
    """The falsifier for a skip that quietly becomes a zero or a win."""
    out = _run(_campaign(tmp_path, a_marker=SKIP, a_score=False))
    assert "mean over" not in out or "mean over 0" in out, out
    # THE ROW, not the page. The footer explains what a `0.0000` means in prose, so scanning the
    # whole output for that string tests the explanation rather than the table -- which is what the
    # first version of this assertion did.
    row = next(line for line in out.splitlines() if line.strip().startswith("demo"))
    assert "0.0000" not in row, f"a skip was rendered as a scored zero: {row!r}"
    assert row.split()[1] == "--", f"arm A shows a number for a task that never ran: {row!r}"


def test_a_real_finish_is_still_a_finish(tmp_path):
    """The other falsifier: normal markers must not start reading as skips."""
    out = _run(_campaign(tmp_path, a_marker=DONE, a_score=True))
    assert "SKIPPED by the operator" not in out, out
    assert "mean over 1 complete pair" in out, out


def test_a_wall_cut_is_still_a_wall_cut(tmp_path):
    """`state=` is read in order and the older classes must keep winning their own marker text."""
    out = _run(_campaign(tmp_path, a_marker="wall=100 rc=124 state=wall_cut attempt=a1\n",
                         a_score=True))
    assert "WALL CLOCK" in out
    assert "SKIPPED by the operator" not in out
