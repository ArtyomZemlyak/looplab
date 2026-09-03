"""The pre-registered statistic is computed by the summary, not by hand each sweep.

§142 fixed "does node 0 carry a compiled kernel" as §115's arm's primary reading. §141 is the
record of what hand-rolling an analysis per sweep costs: an arm sized by free lanes rather than by
the question. §119 put the fact on each probe's own line; this puts the RATE on each card's row,
which is what a pre-registered comparison actually reads — and computing it in one place means it
cannot be computed a different way next time.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUMMARY = REPO / "benchmarks" / "probe_summary.py"


def _probe(root: Path, name: str, card_args: str, kernel: bool, metric: float = 100.0) -> None:
    run = root / name / "runs" / "edge_expansion" / "run"
    (run / "nodes" / "node_0").mkdir(parents=True, exist_ok=True)
    (run / "nodes" / "node_0" / "solver.py").write_text("x\n", encoding="utf-8")
    if kernel:
        (run / "nodes" / "node_0" / "k.pyx").write_text("x\n", encoding="utf-8")
    (run / "events.jsonl").write_text(
        json.dumps({"type": "node_evaluated", "ts": 100.0,
                    "data": {"node_id": 0, "metric": metric}}) + "\n", encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    (root / name / "INSTRUMENT.txt").write_text(
        f"probe:          {name}\ncard_args:      {card_args}\ncard_sha256:    deadbeefdeadbeef\n",
        encoding="utf-8")


def _rows(root: Path) -> list[str]:
    done = subprocess.run([sys.executable, str(SUMMARY), str(root)],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    return [l for l in done.stdout.splitlines() if "node 0 carried a kernel" in l]


def test_each_card_gets_its_own_rate(tmp_path):
    for i in range(3):
        _probe(tmp_path, f"a{i}", "--exploit-best", kernel=True)
    _probe(tmp_path, "a3", "--exploit-best", kernel=False)
    for i in range(2):
        _probe(tmp_path, f"b{i}", "(none -- the shipped card)", kernel=False)
    rows = _rows(tmp_path)
    assert len(rows) == 2, rows
    assert any("3/4 = 75%" in r for r in rows), rows
    assert any("0/2 = 0%" in r for r in rows), rows


def test_a_probe_with_no_node_is_not_counted_as_a_no(tmp_path):
    """MUTATION GUARD: counting a run that has not evaluated yet as "no kernel" would make an arm
    look worse the earlier it is read — the censoring the by-card block already names for spend."""
    _probe(tmp_path, "a0", "--exploit-best", kernel=True)
    _probe(tmp_path, "a1", "--exploit-best", kernel=True)
    run = tmp_path / "a2" / "runs" / "edge_expansion" / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("", encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "a2" / "INSTRUMENT.txt").write_text(
        "probe:          a2\ncard_args:      --exploit-best\n", encoding="utf-8")
    _probe(tmp_path, "b0", "(none -- the shipped card)", kernel=False)
    rows = _rows(tmp_path)
    assert any("2/2 = 100%" in r for r in rows), rows


def test_nothing_is_printed_for_a_card_whose_probes_have_no_nodes(tmp_path):
    run = tmp_path / "a0" / "runs" / "edge_expansion" / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("", encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "a0" / "INSTRUMENT.txt").write_text(
        "probe:          a0\ncard_args:      --exploit-best\n", encoding="utf-8")
    _probe(tmp_path, "b0", "(none -- the shipped card)", kernel=True)
    _probe(tmp_path, "b1", "(none -- the shipped card)", kernel=True)
    rows = _rows(tmp_path)
    assert all("--exploit-best" not in r for r in rows)
