"""Whether the FIRST node carried a compiled kernel is the variable this bench's spread runs on.

§108 measured the gap on `edge_expansion`: a node with a `.pyx` scores a median 166.49 and one
without ~26. §114 found the answer moving, and §119 read it at run level across the seven probes of
2026-09-02 -- six first nodes with a kernel scored 159.8-269.3 and the single one without scored
20.57, with no exceptions. It is a fact about a directory the summary already walks and it appeared
in none of its columns.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUMMARY = REPO / "benchmarks" / "probe_summary.py"


def _probe(root: Path, name: str, files: list[str], metric: float = 5.0) -> None:
    run = root / name / "runs" / "edge_expansion" / "run"
    (run / "nodes" / "node_0").mkdir(parents=True, exist_ok=True)
    for f in files:
        (run / "nodes" / "node_0" / f).write_text("x\n", encoding="utf-8")
    (run / "events.jsonl").write_text(
        json.dumps({"type": "node_evaluated", "ts": 100.0,
                    "data": {"node_id": 0, "metric": metric}}) + "\n", encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")


def _line(root: Path, name: str) -> str:
    done = subprocess.run([sys.executable, str(SUMMARY), str(root)],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    rows = [l for l in done.stdout.splitlines() if l.strip().startswith(f"{name} (")]
    assert rows, done.stdout
    return rows[0]


def test_a_kernel_in_the_first_node_is_named(tmp_path):
    _probe(tmp_path, "withk", ["solver.py", "k.pyx", "setup.py"])
    assert "node 0 kernel" in _line(tmp_path, "withk")


def test_no_kernel_is_named_too(tmp_path):
    """MUTATION GUARD: reporting only the kernel case would make "silent" mean two different
    things -- no kernel, and no node at all."""
    _probe(tmp_path, "nok", ["solver.py"])
    assert "node 0 NO kernel" in _line(tmp_path, "nok")


def test_a_pxd_alone_still_counts(tmp_path):
    _probe(tmp_path, "pxd", ["solver.py", "k.pxd"])
    assert "node 0 kernel" in _line(tmp_path, "pxd")


def test_it_is_the_FIRST_node_that_is_read_not_the_best(tmp_path):
    """The claim is about where the run STARTED; a later kernel is a different fact (§108's
    running-max table)."""
    run = tmp_path / "later" / "runs" / "edge_expansion" / "run"
    (run / "nodes" / "node_0").mkdir(parents=True)
    (run / "nodes" / "node_0" / "solver.py").write_text("x\n", encoding="utf-8")
    (run / "nodes" / "node_1").mkdir(parents=True)
    (run / "nodes" / "node_1" / "k.pyx").write_text("x\n", encoding="utf-8")
    (run / "events.jsonl").write_text(
        json.dumps({"type": "node_evaluated", "ts": 100.0, "data": {"node_id": 0, "metric": 20.0}})
        + "\n"
        + json.dumps({"type": "node_evaluated", "ts": 200.0, "data": {"node_id": 1, "metric": 200.0}})
        + "\n", encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    assert "node 0 NO kernel" in _line(tmp_path, "later")
