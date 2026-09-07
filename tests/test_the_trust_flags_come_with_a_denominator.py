"""A list of flagged probes cannot show a concentration, and the concentration is the finding.

MEASURED 2026-09-02: every trust flag in the corpus is `critic:params_ignored` and every one is on
`discrete_log` -- 4 of its 12 runs, against 0 of 27 `edge_expansion` and 0 of 11 `pde_heat1d`.
Exact one-sided Fisher p = 0.0022. It is a property of the TASK, not of a node.

The count itself moved from four to six within a day, when remDL9 and remDL11 finished, and a
comment that had written "four" down went stale with it. So the summary prints the denominator per
task and nothing hard-codes the count.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUMMARY = REPO / "benchmarks" / "probe_summary.py"


def _probe(root: Path, name: str, task: str, flagged: bool) -> None:
    run = root / name / "runs" / task / "run"
    run.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "node_evaluated", "ts": 100.0, "data": {"node_id": 0, "metric": 5.0}}]
    if flagged:
        rows.append({"type": "reward_hack_suspected", "ts": 101.0,
                     "data": {"node_id": 0, "signal": "critic:params_ignored"}})
    (run / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")


def _run(root: Path) -> str:
    done = subprocess.run([sys.executable, str(SUMMARY), str(root)],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def test_the_denominator_is_printed_per_task(tmp_path):
    _probe(tmp_path, "a1", "discrete_log", True)
    _probe(tmp_path, "a2", "discrete_log", False)
    _probe(tmp_path, "b1", "edge_expansion", False)
    _probe(tmp_path, "b2", "edge_expansion", False)
    out = _run(tmp_path)
    line = [l for l in out.splitlines() if "by task, runs with at least one flag" in l]
    assert line, out
    assert "discrete_log 1/2" in line[0] and "edge_expansion 0/2" in line[0], line[0]


def test_a_task_with_no_flags_is_still_named(tmp_path):
    """MUTATION GUARD: counting only flagged tasks would print `discrete_log 1/1` and hide the
    comparison that makes the concentration visible."""
    _probe(tmp_path, "a1", "discrete_log", True)
    for i in range(3):
        _probe(tmp_path, f"b{i}", "pde_heat1d", False)
    line = [l for l in _run(tmp_path).splitlines() if "by task, runs" in l][0]
    assert "pde_heat1d 0/3" in line, line


def test_nothing_is_printed_when_no_flag_was_raised(tmp_path):
    for i in range(3):
        _probe(tmp_path, f"a{i}", "discrete_log", False)
    assert "by task, runs with at least one flag" not in _run(tmp_path)
