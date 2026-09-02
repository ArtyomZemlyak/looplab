"""The graded number against the number the loop could see.

MEASURED over 44 finished probes, 2026-09-02: median TEST/best-train 0.998, band 0.963–1.260, and
24 of 44 land BELOW 1.0 by a few tenths of a per cent. The card warns hard about the hidden split
-- "anything that fits the train instances SPECIFICALLY ... scores zero where it counts" -- and no
run in this corpus does it. The worst loss is 3.7 % (remEEctl4).

Printed as a SPREAD with both endpoints named, because the claim IS the spread: a run that had
overfitted would sit far below the band, and a median on its own would hide exactly that run.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUMMARY = REPO / "benchmarks" / "probe_summary.py"


def _probe(root: Path, name: str, task: str, train: list[float], test: float) -> None:
    run = root / name / "runs" / task / "run"
    run.mkdir(parents=True, exist_ok=True)
    import json
    lines = [json.dumps({"type": "node_evaluated", "ts": 100.0 + i,
                         "data": {"node_id": i, "metric": m}})
             for i, m in enumerate(train)]
    (run / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    (root / name / "final.json").write_text(json.dumps({"speedup": test}), encoding="utf-8")


def _run(root: Path) -> str:
    done = subprocess.run([sys.executable, str(SUMMARY), str(root)],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _band(out: str) -> dict[str, str]:
    """The band block, selected by its HEADER rather than by a line prefix.

    `discrete_log` also starts a line in the by-card spend block, so a
    `startswith("discrete_log")` filter picked that one instead -- the substring-anchor mistake this
    notebook keeps recording. Anchoring on the header and stopping at the blank line cannot.
    """
    lines = out.splitlines()
    head = next(i for i, l in enumerate(lines) if l.startswith("TEST / best train"))
    rows = {}
    for l in lines[head + 1:]:
        if not l.strip():
            break
        rows[l.split()[0]] = l
    return rows


def test_the_band_is_printed_with_both_ends_named(tmp_path):
    for i, (tr, te) in enumerate([(10.0, 10.0), (10.0, 9.9), (10.0, 10.1),
                                  (10.0, 5.0), (10.0, 10.05), (10.0, 9.95)]):
        _probe(tmp_path, f"p{i}", "discrete_log", [tr], te)
    out = _run(tmp_path)
    assert "TEST / best train" in out, out
    line = _band(out)["discrete_log"]
    assert "0.500 (p3)" in line, f"the overfitted run is not named at the low end: {line}"


def test_the_band_is_per_task_and_not_pooled(tmp_path):
    """§111: pooling hid that `discrete_log` disagrees train-to-test by 37 percentage points while
    `edge_expansion` stays inside 5.2. MUTATION: pool them and this reddens, because one band
    cannot carry two spreads."""
    for i in range(5):
        _probe(tmp_path, f"d{i}", "discrete_log", [10.0], 10.0 + i)      # 1.0 .. 1.4
    for i in range(5):
        _probe(tmp_path, f"e{i}", "edge_expansion", [100.0], 100.0)      # all 1.000
    out = _run(tmp_path)
    rows = _band(out)
    assert "spread 40.0 pp" in rows["discrete_log"], rows["discrete_log"]
    assert "spread 0.0 pp" in rows["edge_expansion"], rows["edge_expansion"]


def test_a_run_without_a_test_score_is_left_out(tmp_path):
    """MUTATION GUARD: counting a probe with no graded number would divide by a missing value."""
    for i in range(5):
        _probe(tmp_path, f"p{i}", "discrete_log", [10.0], 10.0)
    run = tmp_path / "nograde" / "runs" / "discrete_log" / "run"
    run.mkdir(parents=True)
    import json
    (run / "events.jsonl").write_text(
        json.dumps({"type": "node_evaluated", "ts": 1.0, "data": {"node_id": 0, "metric": 7.0}})
        + "\n", encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")
    line = _band(_run(tmp_path))["discrete_log"]
    assert "n= 5" in line, line


def test_too_few_probes_print_nothing(tmp_path):
    """Four points do not make a band; §83's power table is this notebook's standing answer to
    reporting a spread from a handful."""
    for i in range(4):
        _probe(tmp_path, f"p{i}", "discrete_log", [10.0], 10.0)
    assert "TEST / best train" not in _run(tmp_path)
